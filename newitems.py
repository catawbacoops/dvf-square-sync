"""
newitems.py
-----------
New-item pipeline for DVF -> Square Sync.

1. The Price Update records every price-list SKU that is not in Square.
2. lookup job: fetch https://www.dutchvalleyfoods.com/products/details/<SKU>
   for each missing SKU and scrape name, description, image, brand.
3. The user reviews the results in the app and picks which to create.
4. create job: create each item in Square (priced from the case price,
   category copied from existing items in the same price-list section),
   then upload its image.

Jobs run in a background thread; the page polls /api/newitems/status.
State is kept in memory and mirrored to a JSON file so a page refresh
does not lose it (a redeploy or restart does).
"""

import io
import re
import csv
import json
import time
import uuid
import copy
import logging
import threading
from collections import Counter
from pathlib import Path

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

DVF_BASE = "https://www.dutchvalleyfoods.com"
DVF_DETAIL_URL = DVF_BASE + "/products/details/{sku}"
DVF_SEARCH_URL = DVF_BASE + "/search.php"
DVF_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
}
DVF_TIMEOUT = (10, 20)
DVF_DELAY = 0.5          # seconds between DVF requests (be polite)

STATE_FILE = Path("/tmp/dvf_newitems_state.json")

_lock = threading.Lock()
STATE = {
    "missing": [],        # rows from the last price update not found in Square
    "missing_at": None,
    "results": [],        # scraped rows ready for review
    "sections": {},       # {sku: price-list section} for every SKU on the last list
    "job": {"phase": "idle", "done": 0, "total": 0, "message": "", "log": []},
}


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _save():
    try:
        STATE_FILE.write_text(json.dumps(STATE))
    except Exception:
        log.exception("Could not save new-items state")


def _load():
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text())
            STATE.update(data)
            # A job that was running when the process died is not running now
            if STATE["job"].get("phase") in ("lookup", "create"):
                STATE["job"]["phase"] = "idle"
                STATE["job"]["message"] = "Previous job was interrupted (app restarted)."
    except Exception:
        log.exception("Could not load new-items state")


_load()


def job_busy():
    return STATE["job"]["phase"] in ("lookup", "create")


def _job(**kw):
    with _lock:
        STATE["job"].update(kw)
        _save()


def _job_log(msg):
    with _lock:
        STATE["job"]["log"].append(msg)
        STATE["job"]["log"] = STATE["job"]["log"][-300:]
    log.info(msg)


def public_state():
    with _lock:
        return copy.deepcopy({
            "missing_count": len(STATE["missing"]),
            "missing_at": STATE["missing_at"],
            "results": STATE["results"],
            "job": STATE["job"],
        })


# ---------------------------------------------------------------------------
# Step 1: record missing SKUs from a price update
# ---------------------------------------------------------------------------

def _clean(v):
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none", "-") else s


def record_missing(df, sku_map):
    """Store every price-list row whose SKU is not in Square."""
    rows = []
    for _, r in df.iterrows():
        sku = str(r["sku_clean"]).strip().upper()
        if sku in sku_map:
            continue
        rows.append({
            "sku": sku,
            "list_desc": _clean(r.get("desc")),
            "section": _clean(r.get("section")),
            "case_price": _clean(r.get("case_price")),
            "unit_price": _clean(r.get("unit_price")),
            "gp_pct": _clean(r.get("gp_pct")),
            "retail": float(r["retail_calc"]) if r.get("retail_calc") == r.get("retail_calc") else None,
            "case_upc": re.sub(r"\D", "", _clean(r.get("case_upc"))),
            "retail_upc": re.sub(r"\D", "", _clean(r.get("retail_upc"))),
        })
    sections = {str(r["sku_clean"]).strip().upper(): _clean(r.get("section"))
                for _, r in df.iterrows()}
    with _lock:
        STATE["missing"] = rows
        STATE["sections"] = sections
        STATE["missing_at"] = time.strftime("%Y-%m-%d %H:%M")
        if not job_busy():
            STATE["results"] = []
            STATE["job"] = {"phase": "idle", "done": 0, "total": 0, "message": "", "log": []}
        _save()
    return len(rows)


def missing_csv():
    buf = io.StringIO()
    fields = ["sku", "list_desc", "section", "case_price", "unit_price",
              "gp_pct", "retail", "case_upc", "retail_upc"]
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for row in STATE["missing"]:
        w.writerow(row)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Step 2: scrape DVF product pages
# ---------------------------------------------------------------------------

def _meta(soup, prop):
    tag = (soup.find("meta", attrs={"property": prop})
           or soup.find("meta", attrs={"name": prop}))
    return (tag.get("content") or "").strip() if tag else ""


def _text(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def _best_description(soup, fallback):
    """Pick the longest description-looking block on the page."""
    candidates = []
    for el in soup.select('[itemprop="description"], .productView-description, '
                          '#tab-description, [id*="description"], [class*="description"]'):
        if el.name in ("meta", "script", "style"):
            continue
        t = _text(el)
        if 20 <= len(t) <= 6000:
            candidates.append(t)
    best = max(candidates, key=len) if candidates else ""
    return best if len(best) >= len(fallback) else fallback


TAB_LABELS = ("description", "ingredients", "nutrition", "nutrition facts",
              "specifications", "reviews", "allergens", "details", "directions")


def _clean_description(t):
    """Strip tab labels the page puts around the text, e.g.
    'Description Amish Origins... Ingredients' -> 'Amish Origins...'."""
    t = (t or "").strip()
    changed = True
    while changed and t:
        changed = False
        low = t.lower()
        for lab in TAB_LABELS:
            if low.startswith(lab + " ") or low.startswith(lab + ":"):
                t = t[len(lab) + 1:].lstrip(" :"); changed = True; break
            if low.endswith(" " + lab) or low == lab:
                t = t[:len(t) - len(lab)].rstrip(" :"); changed = True; break
    return t.strip()


def _clean_brand(b):
    b = re.sub(r"^\s*view\s+all\s+", "", b or "", flags=re.I)
    b = re.sub(r"\s+products\s*$", "", b, flags=re.I)
    return b.strip()


def _bigger_image(url):
    """BigCommerce URLs end in .<w>.<h>.jpg — ask for a large version."""
    return re.sub(r"\.\d+\.\d+\.(jpe?g|png|gif)", r".1280.1280.\1", url, flags=re.I)


def _search_links(html, base_url):
    """Product page links from a DVF search results page, best first."""
    from urllib.parse import urljoin, urlparse
    soup = BeautifulSoup(html, "html.parser")
    skip = ("search.php", "login", "cart", "account", "/categories", "/brands",
            "wishlist", "compare", "#", "javascript:", "mailto:", "tel:")
    links, seen = [], set()
    # Product cards first (BigCommerce: article.card / .card-title / .productGrid)
    selectors = ["article.card a[href]", ".card-title a[href]", ".productGrid a[href]",
                 ".productList a[href]", "[data-product-id] a[href]", "[class*='product'] a[href]"]
    for sel in selectors:
        for a in soup.select(sel):
            href = urljoin(base_url, a.get("href", ""))
            u = urlparse(href)
            if "dutchvalleyfoods.com" not in u.netloc:
                continue
            if any(x in href.lower() for x in skip) or u.path in ("", "/"):
                continue
            key = u.scheme + "://" + u.netloc + u.path
            if key not in seen:
                seen.add(key); links.append(key)
    return links


def _fetch(s, url, **kw):
    return s.get(url, headers=DVF_HEADERS, timeout=DVF_TIMEOUT, allow_redirects=True, **kw)


def _product_page(resp):
    """(soup, name) if resp is a product page, else (None, None)."""
    if resp.status_code != 200:
        return None, None
    soup = BeautifulSoup(resp.text, "html.parser")
    og_type = _meta(soup, "og:type")
    name = _meta(soup, "og:title") or _text(soup.select_one("h1"))
    if "search.php" in resp.url.lower() or (og_type and og_type != "product") or not name:
        return None, None
    return soup, name


def _name_query(desc):
    """'Jordan Almonds NR3 12/11oz' -> 'Jordan Almonds NR3' (drop pack/size tokens)."""
    t = re.sub(r"\b\d+\s*/\s*[\d.]+\s*\w*\b", " ", desc or "")
    t = re.sub(r"\b[\d.]+\s*(oz|lb|lbs|ct|g|kg|ml|l|gal|pc|pk)\b", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()


def debug_search(q):
    """What the server actually receives from DVF's search for q."""
    from urllib.parse import urljoin
    r = _fetch(requests, DVF_SEARCH_URL, params={"search_query": q})
    soup = BeautifulSoup(r.text, "html.parser")
    hrefs = []
    for a in soup.find_all("a", href=True):
        h = urljoin(r.url, a["href"])
        if "dutchvalleyfoods.com" in h and h not in hrefs:
            hrefs.append(h)
    scripts = [sc.get("src") for sc in soup.find_all("script", src=True)]
    body_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    i = body_text.lower().find("result")
    return {
        "query": q,
        "status": r.status_code,
        "final_url": r.url,
        "html_length": len(r.text),
        "title": _text(soup.title),
        "query_in_html": q in r.text,
        "cards_found": len(_search_links(r.text, r.url)),
        "card_links": _search_links(r.text, r.url)[:10],
        "all_links_count": len(hrefs),
        "links_not_menu_sample": [h for h in hrefs if "/categories" not in h][-40:],
        "script_sources": [x for x in scripts if x][:40],
        "text_near_results": body_text[max(0, i - 200): i + 300] if i >= 0 else "",
        "forms": [{"action": f.get("action"), "inputs": [inp.get("name") for inp in f.find_all("input")]}
                  for f in soup.find_all("form")][:5],
    }


def scrape_dvf(sku, session=None, name_hint=""):
    """
    Return a dict of product details for one SKU, or found=False.
    Tries the direct /products/details/<SKU> page first; if that is
    missing, searches the site for the SKU like a person would.
    """
    s = session or requests
    url = DVF_DETAIL_URL.format(sku=sku)
    out = {"sku": sku, "found": False, "url": url, "error": "", "method": ""}
    resp = soup = name = None
    try:
        resp = _fetch(s, url)
        soup, name = _product_page(resp)
        if soup is not None:
            out["method"] = "direct"
    except requests.RequestException as e:
        out["error"] = f"request failed: {e}"

    if soup is None:
        # Fallback: site search — SKU, SKU without leading zeros, then name
        queries = [sku]
        if sku.lstrip("0") and sku.lstrip("0") != sku:
            queries.append(sku.lstrip("0"))
        nq = _name_query(name_hint)
        if nq and len(nq) >= 4:
            queries.append(nq)
        tried = []
        any_results = False
        for q in queries:
            try:
                sresp = _fetch(s, DVF_SEARCH_URL, params={"search_query": q})
            except requests.RequestException as e:
                tried.append(f"{q}: failed")
                continue
            out.setdefault("search_url", sresp.url)
            psoup, pname = _product_page(sresp)
            if psoup is not None and (sku in (_meta(psoup, "og:image") or "") or sku in sresp.text):
                resp, soup, name = sresp, psoup, pname
                out["method"] = f"search '{q}' (direct hit)"
                break
            if sresp.status_code != 200:
                tried.append(f"{q}: HTTP {sresp.status_code}")
                continue
            candidates = _search_links(sresp.text, sresp.url)
            any_results = any_results or bool(candidates)
            tried.append(f"{q}: {len(candidates)} result(s) — {sresp.url}")
            for link in candidates[:6]:
                time.sleep(DVF_DELAY)
                try:
                    presp = _fetch(s, link)
                except requests.RequestException:
                    continue
                psoup, pname = _product_page(presp)
                if psoup is None:
                    continue
                if sku in (_meta(psoup, "og:image") or "") or sku in presp.text:
                    resp, soup, name = presp, psoup, pname
                    out["method"] = f"search '{q}'"
                    break
            if soup is not None:
                break
            time.sleep(DVF_DELAY)
        out["searches"] = tried
        if soup is None:
            out["error"] = ("searched DVF, no page shows this SKU" if any_results
                            else "no search results on DVF")

    if soup is None:
        return out

    out["final_url"] = resp.url
    out["error"] = ""

    image = _meta(soup, "og:image")
    page_text = resp.text
    # Confirm the page really is this SKU: DVF image files start with the SKU
    sku_seen = (sku in (image or "")) or (sku in page_text)

    brand = (_text(soup.select_one('[itemprop="brand"]'))
             or _text(soup.select_one(".productView-brand")))
    crumbs = [_text(li) for li in soup.select(".breadcrumbs li, nav.breadcrumb li, .breadcrumb li")]
    crumbs = [c for c in crumbs if c and c.lower() not in ("home", "products")]
    if crumbs and crumbs[-1].lower() == name.lower():
        crumbs = crumbs[:-1]

    out.update({
        "found": True,
        "name": name,
        "description": _clean_description(_best_description(soup, _meta(soup, "og:description"))),
        "image": image,
        "image_large": _bigger_image(image) if image else "",
        "brand": _clean_brand(brand),
        "breadcrumb": " > ".join(crumbs),
        "availability": _meta(soup, "og:availability"),
        "sku_verified": bool(sku_seen),
    })
    return out


def _lookup_worker(rows):
    session = requests.Session()
    results = []
    total = len(rows)
    _job(phase="lookup", done=0, total=total, message="Looking up items on DVF…", log=[])
    for i, row in enumerate(rows, 1):
        info = scrape_dvf(row["sku"], session=session, name_hint=row.get("list_desc", ""))
        merged = dict(row)
        merged.update(info)
        if not info.get("found"):
            merged["name"] = row["list_desc"]
        if not merged.get("description"):
            merged["description"] = ""
        # Default selection: found on DVF, verified, and has a price
        merged["selected"] = bool(info.get("found") and info.get("sku_verified")
                                  and merged.get("retail"))
        results.append(merged)
        with _lock:
            STATE["results"] = results
            STATE["job"]["done"] = i
        if i % 25 == 0 or i == total:
            _save()
        time.sleep(DVF_DELAY)

    found = sum(1 for r in results if r.get("found"))
    _job(phase="review", message=f"Found {found} of {total} on DVF. Review and create below.")


def _retry_worker(rows):
    """Re-look-up only the given rows and update them in place."""
    session = requests.Session()
    total = len(rows)
    _job(phase="lookup", done=0, total=total, message="Retrying items not found…", log=[])
    for i, row in enumerate(rows, 1):
        base = next((m for m in STATE["missing"] if m["sku"] == row["sku"]), row)
        info = scrape_dvf(row["sku"], session=session, name_hint=base.get("list_desc", ""))
        merged = dict(base)
        merged.update(info)
        if not info.get("found"):
            merged["name"] = base.get("list_desc", "")
        merged["selected"] = bool(info.get("found") and info.get("sku_verified") and merged.get("retail"))
        with _lock:
            for j, r in enumerate(STATE["results"]):
                if r["sku"] == row["sku"]:
                    STATE["results"][j] = merged
            STATE["job"]["done"] = i
        if i % 25 == 0 or i == total:
            _save()
        time.sleep(DVF_DELAY)
    found = sum(1 for r in STATE["results"] if r.get("found"))
    _job(phase="review", message=f"Found {found} of {len(STATE['results'])} on DVF. Review and create below.")


def start_retry_not_found():
    if job_busy():
        return False, "A job is already running."
    rows = [r for r in STATE["results"] if not r.get("found") and not r.get("square_id")]
    if not rows:
        return False, "Nothing to retry."
    threading.Thread(target=_retry_worker, args=(rows,), daemon=True).start()
    return True, f"Retrying {len(rows)} SKU(s)."


def start_lookup(skus=None):
    if job_busy():
        return False, "A job is already running."
    rows = STATE["missing"]
    if skus:
        wanted = {s.upper() for s in skus}
        rows = [r for r in rows if r["sku"] in wanted]
    if not rows:
        return False, "No missing SKUs. Run a Price Update first."
    threading.Thread(target=_lookup_worker, args=(rows,), daemon=True).start()
    return True, f"Started lookup of {len(rows)} SKU(s)."


# ---------------------------------------------------------------------------
# Step 3/4: create items in Square
# ---------------------------------------------------------------------------

def _square_list(base, headers, timeout, types):
    objs, cursor = [], None
    while True:
        params = {"types": types}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{base}/catalog/list", headers=headers, params=params, timeout=timeout)
        r.raise_for_status()
        d = r.json()
        objs += d.get("objects", [])
        cursor = d.get("cursor")
        if not cursor:
            return objs


def _item_categories(item):
    data = item.get("item_data", {})
    cats = [c["id"] for c in data.get("categories", []) if c.get("id")]
    if not cats and data.get("category_id"):
        cats = [data["category_id"]]
    return tuple(cats)


def build_section_categories(sku_sections, items):
    """
    For each price-list section, find the category set most used by
    existing Square items whose SKUs are in that section.
    Returns ({section: (cat_id, ...)}, {sku: item}).
    """
    sku_to_item = {}
    for it in items:
        for v in it.get("item_data", {}).get("variations", []):
            sku = v.get("item_variation_data", {}).get("sku", "").strip().upper()
            if sku:
                sku_to_item[sku] = it
    votes = {}
    for sku, section in sku_sections.items():
        it = sku_to_item.get(sku)
        if it is None or not section:
            continue
        cats = _item_categories(it)
        if cats:
            votes.setdefault(section, Counter())[cats] += 1
    return {sec: c.most_common(1)[0][0] for sec, c in votes.items()}, sku_to_item


def _valid_gtin(code):
    """Check digit validation for UPC-A / EAN-13 / GTIN-14."""
    if not code or not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    digits = [int(c) for c in code]
    check = digits.pop()
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return (10 - total % 10) % 10 == check


def _item_payload(row, currency, category_ids, upc):
    variation = {
        "type": "ITEM_VARIATION",
        "id": "#variation",
        "present_at_all_locations": True,
        "item_variation_data": {
            "name": "Regular",
            "sku": row["sku"],
            "pricing_type": "FIXED_PRICING",
            "price_money": {"amount": int(round(row["retail"] * 100)), "currency": currency},
            "sellable": True,
            "stockable": True,
        },
    }
    if upc:
        variation["item_variation_data"]["upc"] = upc
    item_data = {
        "name": (row.get("name") or row["list_desc"] or row["sku"])[:512],
        "variations": [variation],
        "product_type": "REGULAR",
    }
    desc = (row.get("description") or "").strip()
    if desc:
        item_data["description"] = desc[:4000]
    if category_ids:
        item_data["categories"] = [{"id": c} for c in category_ids]
        item_data["reporting_category"] = {"id": category_ids[0]}
    return {
        "idempotency_key": str(uuid.uuid4()),
        "object": {
            "type": "ITEM",
            "id": "#item",
            "present_at_all_locations": True,
            "item_data": item_data,
        },
    }


def _upload_image(base, headers, item_id, row, session):
    for url in [row.get("image_large"), row.get("image")]:
        if not url:
            continue
        try:
            img = session.get(url, headers=DVF_HEADERS, timeout=DVF_TIMEOUT)
            if img.status_code != 200 or len(img.content) < 500:
                continue
            ctype = img.headers.get("Content-Type", "image/jpeg").split(";")[0]
            if ctype not in ("image/jpeg", "image/png", "image/gif", "image/pjpeg"):
                ctype = "image/jpeg"
            req = {
                "idempotency_key": str(uuid.uuid4()),
                "object_id": item_id,
                "is_primary": True,
                "image": {"type": "IMAGE", "id": "#image",
                          "image_data": {"name": row.get("name", row["sku"])[:255]}},
            }
            h = {k: v for k, v in headers.items() if k.lower() != "content-type"}
            ext = "png" if "png" in ctype else ("gif" if "gif" in ctype else "jpg")
            r = requests.post(
                f"{base}/catalog/images", headers=h,
                files={"request": (None, json.dumps(req), "application/json"),
                       "file": (f"{row['sku']}.{ext}", img.content, ctype)},
                timeout=(10, 60))
            if r.ok:
                return True, ""
            return False, f"image upload failed ({r.status_code}): {r.text[:150]}"
        except requests.RequestException as e:
            return False, f"image download/upload error: {e}"
    return False, "no image"


def _create_worker(rows, base, headers, timeout, currency):
    total = len(rows)
    _job(phase="create", done=0, total=total, message="Preparing…", log=[])
    try:
        items = _square_list(base, headers, timeout, "ITEM")
        section_cats, sku_to_item = build_section_categories(STATE.get("sections", {}), items)
    except Exception as e:
        _job(phase="review", message=f"❌ Could not read Square catalog: {e}")
        return

    session = requests.Session()
    created = skipped = failed = no_img = 0
    _job(message="Creating items in Square…")
    for i, row in enumerate(rows, 1):
        sku = row["sku"]
        if sku in sku_to_item:
            skipped += 1
            _set_result(sku, status="already in Square")
            _job(done=i)
            continue
        if not row.get("retail") or row["retail"] <= 0:
            failed += 1
            _set_result(sku, status="no valid price")
            _job(done=i)
            continue

        cats = section_cats.get(row.get("section", ""), ())
        upc = next((u for u in (row.get("case_upc"), row.get("retail_upc")) if _valid_gtin(u)), "")
        payload = _item_payload(row, currency, cats, upc)
        r = requests.post(f"{base}/catalog/object", headers=headers, json=payload, timeout=timeout)
        if not r.ok and upc:
            # Retry without the UPC in case Square rejects the code
            payload = _item_payload(row, currency, cats, "")
            r = requests.post(f"{base}/catalog/object", headers=headers, json=payload, timeout=timeout)
        if not r.ok:
            failed += 1
            err = r.text[:200]
            _set_result(sku, status=f"failed: {err}")
            _job_log(f"❌ {sku}: {err}")
            _job(done=i)
            continue

        item_id = r.json()["catalog_object"]["id"]
        created += 1
        ok, img_err = _upload_image(base, headers, item_id, row, session)
        if not ok:
            no_img += 1
        status = "created" + ("" if ok else f" (no image: {img_err})")
        if not cats:
            status += " (no category)"
        _set_result(sku, status=status, square_id=item_id, selected=False)
        _job(done=i)
        time.sleep(0.2)

    msg = f"✅ Created {created} item(s)."
    if skipped:
        msg += f" {skipped} already in Square."
    if failed:
        msg += f" ❌ {failed} failed."
    if no_img:
        msg += f" ⚠️ {no_img} created without an image."
    _job(phase="review", message=msg)


def _set_result(sku, **kw):
    with _lock:
        for r in STATE["results"]:
            if r["sku"] == sku:
                r.update(kw)
                break


def start_create(skus, base, headers, timeout, currency):
    if job_busy():
        return False, "A job is already running."
    wanted = {s.upper() for s in skus}
    rows = [r for r in STATE["results"] if r["sku"] in wanted]
    if not rows:
        return False, "No items selected."
    threading.Thread(target=_create_worker,
                     args=(rows, base, headers, timeout, currency),
                     daemon=True).start()
    return True, f"Creating {len(rows)} item(s)…"
