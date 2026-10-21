"""
app.py
------
Flask web app for Dutch Valley Foods → Square sync.
Hosted on Render. Handles out-of-stock and discontinued reports.

Environment variables (set in Render dashboard):
  SQUARE_ACCESS_TOKEN   — your Square access token
  CURRENCY              — e.g. USD
"""

import os
import re
import copy
import json
import logging
import tempfile
from pathlib import Path
from datetime import datetime

import requests
import pandas as pd
from flask import Flask, request, jsonify, render_template_string, Response, send_file

import newitems
import catmatch

# ---------------------------------------------------------------------------
# Config from environment variables
# ---------------------------------------------------------------------------
SQUARE_ACCESS_TOKEN = os.environ.get("SQUARE_ACCESS_TOKEN", "")
CURRENCY            = os.environ.get("CURRENCY", "USD")
SQUARE_BASE_URL     = "https://connect.squareup.com/v2"
SQUARE_HEADERS      = {
    "Authorization":  f"Bearer {SQUARE_ACCESS_TOKEN}",
    "Content-Type":   "application/json",
    "Square-Version": "2024-01-18",
}
SQUARE_TIMEOUT = (10, 25)  # (connect timeout, read timeout) seconds

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Square API helpers
# ---------------------------------------------------------------------------

def get_all_catalog_items():
    """Return {sku_upper: variation_object} for all catalog variations."""
    sku_map = {}
    cursor = None
    while True:
        params = {"types": "ITEM_VARIATION"}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(f"{SQUARE_BASE_URL}/catalog/list",
                            headers=SQUARE_HEADERS, params=params, timeout=SQUARE_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        for obj in data.get("objects", []):
            sku = obj.get("item_variation_data", {}).get("sku", "")
            if sku:
                sku_map[sku.strip().upper()] = obj
        cursor = data.get("cursor")
        if not cursor:
            break
    return sku_map


def set_sellable(skus, sku_map, sellable: bool):
    """Set sellable=True or sellable=False on a list of SKUs."""
    objects = []
    for sku in skus:
        variation_obj = copy.deepcopy(sku_map[sku])
        for field in ["updated_at", "created_at", "is_deleted", "catalog_v1_ids"]:
            variation_obj.pop(field, None)
        variation_obj.setdefault("item_variation_data", {})["sellable"] = sellable
        objects.append(variation_obj)

    for i in range(0, len(objects), 1000):
        payload = {
            "idempotency_key": f"sellable-{sellable}-{datetime.utcnow().timestamp()}-{i}",
            "batches": [{"objects": objects[i:i+1000]}],
        }
        resp = requests.post(f"{SQUARE_BASE_URL}/catalog/batch-upsert",
                             headers=SQUARE_HEADERS, json=payload, timeout=SQUARE_TIMEOUT)
        resp.raise_for_status()
    label = "Sellable=Y" if sellable else "Sellable=N"
    log.info(f"Set {label} on {len(objects)} item(s).")


def archive_items(item_ids):
    """Fetch full item objects and set is_archived=True."""
    if not item_ids:
        return
    objects = []
    id_list = list(set(item_ids))
    for i in range(0, len(id_list), 1000):
        resp = requests.post(
            f"{SQUARE_BASE_URL}/catalog/batch-retrieve",
            headers=SQUARE_HEADERS,
            json={"object_ids": id_list[i:i+1000], "include_related_objects": False},
            timeout=SQUARE_TIMEOUT
        )
        resp.raise_for_status()
        for obj in resp.json().get("objects", []):
            full_obj = copy.deepcopy(obj)
            for field in ["updated_at", "created_at", "is_deleted", "catalog_v1_ids"]:
                full_obj.pop(field, None)
            full_obj.setdefault("item_data", {})["is_archived"] = True
            objects.append(full_obj)

    for i in range(0, len(objects), 1000):
        payload = {
            "idempotency_key": f"archive-{datetime.utcnow().timestamp()}-{i}",
            "batches": [{"objects": objects[i:i+1000]}],
        }
        resp = requests.post(f"{SQUARE_BASE_URL}/catalog/batch-upsert",
                             headers=SQUARE_HEADERS, json=payload, timeout=SQUARE_TIMEOUT)
        resp.raise_for_status()
    log.info(f"Archived {len(objects)} item(s).")



def update_prices(df, sku_map):
    """
    Update Square item variation prices from the wholesale price list.
    Retail price = case_price / (1 - gp_pct)  (sold by the case only)
    """
    import copy
    messages = []
    updated, skipped, missing = 0, 0, []

    objects = []
    for _, row in df.iterrows():
        sku = str(row["sku_clean"]).strip().upper()
        if sku not in sku_map:
            missing.append(sku)
            continue

        retail = row.get("retail_calc")
        if not retail or pd.isna(retail) or retail <= 0:
            skipped += 1
            continue

        price_cents = int(round(retail * 100))

        variation_obj = copy.deepcopy(sku_map[sku])
        for field in ["updated_at", "created_at", "is_deleted", "catalog_v1_ids"]:
            variation_obj.pop(field, None)
        variation_obj.setdefault("item_variation_data", {})["price_money"] = {
            "amount": price_cents,
            "currency": CURRENCY,
        }
        variation_obj["item_variation_data"]["pricing_type"] = "FIXED_PRICING"
        objects.append(variation_obj)

    # Batch upsert in groups of 1000
    for i in range(0, len(objects), 1000):
        payload = {
            "idempotency_key": f"price-update-{datetime.utcnow().timestamp()}-{i}",
            "batches": [{"objects": objects[i:i+1000]}],
        }
        resp = requests.post(f"{SQUARE_BASE_URL}/catalog/batch-upsert",
                             headers=SQUARE_HEADERS, json=payload, timeout=SQUARE_TIMEOUT)
        resp.raise_for_status()
        updated += len(objects[i:i+1000])

    if missing:
        messages.append(f"⚠️  {len(missing)} SKU(s) not found in Square: {', '.join(missing[:20])}{'...' if len(missing) > 20 else ''}")
    if skipped:
        messages.append(f"ℹ️  {skipped} row(s) skipped — no valid price.")
    messages.append(f"✅  Updated prices on {updated} item(s).")
    return messages


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------

def read_price_list(file_storage):
    """
    Read the DVF wholesale price list.
    Structure:
      Row 0: blank
      Row 1: date
      Row 2-3: split headers
      Row 4+: data (category header rows have no price — skipped)

    Columns:
      0  = SKU (format "123 456" -> strip space -> "123456")
      1  = Description
      2  = Case price
      4  = Unit price
      6  = Suggested retail
      8  = GP%
      11 = Case UPC
      12 = Retail UPC
    """
    filename = file_storage.filename or ""
    suffix   = Path(filename).suffix.lower()

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        file_storage.save(tmp.name)
        tmp_path = Path(tmp.name)

    try:
        engine = "xlrd" if suffix == ".xls" else "openpyxl"
        df = pd.read_excel(tmp_path, header=None, dtype=str, engine=engine)

        # Section headers (e.g. "AMISH ORIGINS / SALVES & SOAPS") are rows with
        # a description but no SKU and no price. Carry each one down to its items.
        body = df.iloc[4:].copy()
        is_header = body[0].isna() & body[1].notna() & body[2].isna()
        body["section"] = body[1].where(is_header).ffill().str.strip()

        # Take only the columns we need
        data = body[[0, 1, 2, 4, 6, 8, 11, 12, "section"]].copy()
        data.columns = ["sku", "desc", "case_price", "unit_price", "sug_retail",
                        "gp_pct", "case_upc", "retail_upc", "section"]

        # Drop category header rows (no SKU or no price)
        data = data.dropna(subset=["sku", "unit_price"])
        data = data[data["unit_price"].str.strip().str.replace(".", "", 1).str.isnumeric()]

        # Clean SKU — strip space, uppercase
        data["sku_clean"] = data["sku"].str.replace(" ", "").str.strip().str.upper()

        # Clean GP% -> decimal  e.g. "30%" -> 0.30
        data["gp_clean"] = pd.to_numeric(
            data["gp_pct"].str.replace("%", "").str.strip(), errors="coerce"
        ) / 100

        # Case price as float (column 2). Items are sold BY THE CASE only.
        data["case_price_num"] = pd.to_numeric(data["case_price"], errors="coerce")

        # Calculate retail = case_price / (1 - gp%)
        # Guard against GP% >= 100% (would divide by zero or go negative)
        data.loc[data["gp_clean"] >= 1, "gp_clean"] = float("nan")
        data["retail_calc"] = (data["case_price_num"] / (1 - data["gp_clean"])).round(2)

        # Drop rows where calculation failed
        data = data.dropna(subset=["retail_calc"])
        data = data[data["retail_calc"] > 0]

        return data

    finally:
        tmp_path.unlink(missing_ok=True)


def read_vendor_file(file_storage):
    """
    Read an uploaded .xls or .xlsx vendor file.
    Row 0: title row (skipped)
    Row 1: headers (Item | Description | ...)
    Row 2+: data

    .xls files are read directly with xlrd.
    .xlsx files are read with openpyxl.
    """
    filename = file_storage.filename or ""
    suffix   = Path(filename).suffix.lower()

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        file_storage.save(tmp.name)
        tmp_path = Path(tmp.name)

    try:
        if suffix == ".xls":
            engine = "xlrd"
        else:
            engine = "openpyxl"

        df = pd.read_excel(tmp_path, header=1, dtype=str, engine=engine)
        df.columns = [c.strip() for c in df.columns]

        # Normalise SKU column name
        if "Item Number" in df.columns:
            df = df.rename(columns={"Item Number": "Item"})

        df = df.dropna(subset=["Item"])
        df["Item"] = df["Item"].str.strip()
        return df

    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------

def sync_out_of_stock(df, sku_map):
    """Mark OOS items as Sellable=N, reset previously OOS items to Sellable=Y."""
    messages = []

    oos_skus, missing = [], []
    for _, row in df.iterrows():
        sku = row["Item"].upper()
        if sku in sku_map:
            oos_skus.append(sku)
        else:
            missing.append(sku)

    if missing:
        messages.append(f"⚠️  {len(missing)} SKU(s) not found in Square: {', '.join(missing[:20])}{'...' if len(missing) > 20 else ''}")

    if oos_skus:
        set_sellable(oos_skus, sku_map, sellable=False)
        messages.append(f"✅  Set Sellable=N on {len(oos_skus)} item(s).")

    # Reset previously OOS items no longer on the list
    oos_set = set(oos_skus)
    previously_oos = [
        sku for sku, obj in sku_map.items()
        if obj.get("item_variation_data", {}).get("sellable") is False
        and sku not in oos_set
    ]
    if previously_oos:
        set_sellable(previously_oos, sku_map, sellable=True)
        messages.append(f"🔄  Reset Sellable=Y on {len(previously_oos)} previously OOS item(s).")

    return messages


def sync_discontinued(df, sku_map):
    """Archive discontinued items in Square."""
    messages = []

    found_item_ids, missing = [], []
    for _, row in df.iterrows():
        sku = row["Item"].upper()
        if sku in sku_map:
            item_id = sku_map[sku]["item_variation_data"]["item_id"]
            found_item_ids.append(item_id)
        else:
            missing.append(sku)

    if missing:
        messages.append(f"⚠️  {len(missing)} SKU(s) not found in Square: {', '.join(missing[:20])}{'...' if len(missing) > 20 else ''}")

    if found_item_ids:
        archive_items(found_item_ids)
        messages.append(f"✅  Archived {len(found_item_ids)} item(s) in Square.")
    else:
        messages.append("ℹ️  No matching items found to archive.")

    return messages


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/sync", methods=["POST"])
def sync():
    if "file" not in request.files:
        return jsonify({"status": "error", "messages": ["No file uploaded."]}), 400

    file = request.files["file"]
    sync_type = request.form.get("type", "")

    if sync_type not in ("out_of_stock", "discontinued"):
        return jsonify({"status": "error", "messages": ["Invalid sync type."]}), 400

    if not SQUARE_ACCESS_TOKEN:
        return jsonify({"status": "error", "messages": ["SQUARE_ACCESS_TOKEN not configured."]}), 500

    try:
        df = read_vendor_file(file)
        messages = [f"📄  Read {len(df)} items from {file.filename}"]

        sku_map = get_all_catalog_items()
        messages.append(f"🔗  Loaded {len(sku_map)} SKUs from Square.")

        if sync_type == "out_of_stock":
            messages += sync_out_of_stock(df, sku_map)
        else:
            messages += sync_discontinued(df, sku_map)

        return jsonify({"status": "ok", "messages": messages})

    except Exception as e:
        log.exception("Sync error")
        return jsonify({"status": "error", "messages": [f"❌  Error: {str(e)}"]}), 500


@app.route("/api/sync/prices", methods=["POST"])
def sync_prices():
    if "file" not in request.files:
        return jsonify({"status": "error", "messages": ["No file uploaded."]}), 400

    if not SQUARE_ACCESS_TOKEN:
        return jsonify({"status": "error", "messages": ["SQUARE_ACCESS_TOKEN not configured."]}), 500

    file = request.files["file"]
    try:
        df = read_price_list(file)
        messages = [f"📄  Read {len(df)} priced items from {file.filename}"]

        sku_map = get_all_catalog_items()
        messages.append(f"🔗  Loaded {len(sku_map)} SKUs from Square.")

        messages += update_prices(df, sku_map)

        n_missing = newitems.record_missing(df, sku_map)
        if n_missing:
            messages.append(f"🆕  {n_missing} new SKU(s) saved — use New Items below to look them up on DVF.")
        return jsonify({"status": "ok", "messages": messages})

    except Exception as e:
        log.exception("Price sync error")
        return jsonify({"status": "error", "messages": [f"❌  Error: {str(e)}"]}), 500


@app.route("/api/newitems/state")
def newitems_state():
    return jsonify(newitems.public_state())


@app.route("/api/newitems/missing.csv")
def newitems_missing_csv():
    return Response(newitems.missing_csv(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=new_skus_not_in_square.csv"})


@app.route("/api/newitems/probe")
def newitems_probe():
    """Look up one SKU on DVF without changing anything. ?sku=005030"""
    sku = re.sub(r"\s", "", request.args.get("sku", "")).upper()
    if not sku:
        return jsonify({"error": "add ?sku=XXXXXX"}), 400
    hint = next((m.get("list_desc", "") for m in newitems.STATE["missing"] if m["sku"] == sku),
                request.args.get("name", ""))
    return jsonify(newitems.scrape_dvf(sku, name_hint=hint))


@app.route("/api/newitems/debug_search")
def newitems_debug_search():
    """Show what DVF's search returns to the server. ?q=633006"""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "add ?q=SKU"}), 400
    try:
        return jsonify(newitems.debug_search(q))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _square_lister(types):
    return newitems._square_list(SQUARE_BASE_URL, SQUARE_HEADERS, SQUARE_TIMEOUT, types)


@app.route("/api/newitems/category_preview")
def newitems_category_preview():
    """
    Preview only — changes nothing. ?sku=100120
    Shows DVF's category path, the matching Square category, and where the
    item sits in Square now (if it exists).
    """
    sku = re.sub(r"\s", "", request.args.get("sku", "")).upper()
    if not sku:
        return jsonify({"error": "add ?sku=XXXXXX"}), 400
    try:
        prod, err = newitems.gql_product(sku)
        tree = catmatch.load_tree(_square_lister, force=bool(request.args.get("refresh")))
        m = catmatch.match(prod, tree) if prod else None
        out = {
            "sku": sku,
            "dvf_name": (prod or {}).get("name"),
            "dvf_category_paths": catmatch.dvf_paths(prod) if prod else [],
            "dvf_error": err,
            "square_categories_loaded": len(tree["cats"]),
            "items_store_categories_as": tree["pattern"],
            "now_in_square": catmatch.item_category_path(sku, tree),
            "proposed": None,
        }
        if m:
            cats = tree["cats"]
            node = cats[m["cat_id"]]
            out["proposed"] = {
                "square_path": " > ".join(m["square_path"]),
                "fully_matched": not m["missing"],
                "dvf_levels_missing_in_square": m["missing"],
                "existing_subcategories_here": sorted(cats[k]["name"] for k in tree["children"].get(node["id"], [])),
                "category_ids_to_assign": m["category_ids"],
            }
        return jsonify(out)
    except Exception as e:
        log.exception("category preview")
        return jsonify({"error": str(e)}), 500


@app.route("/api/newitems/lookup", methods=["POST"])
def newitems_lookup():
    body = request.get_json(silent=True) or {}
    ok, msg = newitems.start_lookup(body.get("skus"))
    return jsonify({"status": "ok" if ok else "error", "message": msg}), (200 if ok else 409)


@app.route("/api/newitems/retry", methods=["POST"])
def newitems_retry():
    ok, msg = newitems.start_retry_not_found()
    return jsonify({"status": "ok" if ok else "error", "message": msg}), (200 if ok else 409)


@app.route("/api/newitems/fix_images", methods=["POST"])
def newitems_fix_images():
    if not SQUARE_ACCESS_TOKEN:
        return jsonify({"status": "error", "message": "SQUARE_ACCESS_TOKEN not configured."}), 500
    body = request.get_json(silent=True) or {}
    skus = body.get("skus") or []
    if isinstance(skus, str):
        skus = re.split(r"[,\s]+", skus)
    skus = [x for x in skus if x]
    ok, msg = newitems.start_fix_images(SQUARE_BASE_URL, SQUARE_HEADERS, SQUARE_TIMEOUT, skus)
    return jsonify({"status": "ok" if ok else "error", "message": msg}), (200 if ok else 409)


@app.route("/api/newitems/fix_categories", methods=["POST"])
def newitems_fix_categories():
    if not SQUARE_ACCESS_TOKEN:
        return jsonify({"status": "error", "message": "SQUARE_ACCESS_TOKEN not configured."}), 500
    body = request.get_json(silent=True) or {}
    skus = body.get("skus") or []
    if isinstance(skus, str):
        skus = re.split(r"[,\s]+", skus)
    ok, msg = newitems.start_fix_categories(SQUARE_BASE_URL, SQUARE_HEADERS, SQUARE_TIMEOUT,
                                            [x for x in skus if x])
    return jsonify({"status": "ok" if ok else "error", "message": msg}), (200 if ok else 409)


@app.route("/api/newitems/weights", methods=["POST"])
def newitems_weights():
    if not SQUARE_ACCESS_TOKEN:
        return jsonify({"status": "error", "message": "SQUARE_ACCESS_TOKEN not configured."}), 500
    body = request.get_json(silent=True) or {}
    skus = body.get("skus") or []
    if isinstance(skus, str):
        skus = re.split(r"[,\s]+", skus)
    ok, msg = newitems.start_weights(SQUARE_BASE_URL, SQUARE_HEADERS, SQUARE_TIMEOUT,
                                     [x for x in skus if x], body.get("unit", "lb"))
    return jsonify({"status": "ok" if ok else "error", "message": msg}), (200 if ok else 409)


@app.route("/api/newitems/weights.xlsx")
def newitems_weights_file():
    if not newitems.WEIGHTS_FILE.exists():
        return jsonify({"error": "No weight file yet."}), 404
    return send_file(newitems.WEIGHTS_FILE, as_attachment=True,
                     download_name="square_import_new_item_weights.xlsx")


@app.route("/api/newitems/create", methods=["POST"])
def newitems_create():
    if not SQUARE_ACCESS_TOKEN:
        return jsonify({"status": "error", "message": "SQUARE_ACCESS_TOKEN not configured."}), 500
    body = request.get_json(silent=True) or {}
    skus = body.get("skus") or []
    ok, msg = newitems.start_create(skus, SQUARE_BASE_URL, SQUARE_HEADERS, SQUARE_TIMEOUT, CURRENCY)
    return jsonify({"status": "ok" if ok else "error", "message": msg}), (200 if ok else 409)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DVF → Square Sync</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg:        #0f1117;
    --surface:   #171b24;
    --border:    #252a35;
    --text:      #e2e8f0;
    --muted:     #64748b;
    --green:     #22c55e;
    --green-dim: #16a34a22;
    --amber:     #f59e0b;
    --amber-dim: #d9770622;
    --blue:      #3b82f6;
    --blue-dim:  #3b82f622;
    --red:       #ef4444;
    --red-dim:   #ef444422;
    --mono:      'IBM Plex Mono', monospace;
    --sans:      'IBM Plex Sans', sans-serif;
  }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 40px 20px;
  }
  .container { width: 100%; max-width: 680px; }

  header {
    display: flex;
    align-items: center;
    gap: 14px;
    margin-bottom: 40px;
  }
  .logo {
    width: 44px; height: 44px;
    background: var(--green-dim);
    border: 1px solid #16a34a55;
    border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    font-size: 22px;
  }
  header h1 {
    font-family: var(--mono);
    font-size: 16px;
    font-weight: 600;
    letter-spacing: 0.05em;
  }
  header p { font-size: 12px; color: var(--muted); margin-top: 2px; }

  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 24px;
    margin-bottom: 16px;
  }

  .card-title {
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 600;
    margin-bottom: 6px;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .card-desc {
    font-size: 13px;
    color: var(--muted);
    line-height: 1.6;
    margin-bottom: 20px;
  }

  .tags {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-bottom: 16px;
  }
  .tag {
    font-family: var(--mono);
    font-size: 10px;
    padding: 3px 8px;
    border-radius: 4px;
    border: 1px solid var(--border);
    color: var(--muted);
  }
  .tag.green { border-color: #16a34a55; color: var(--green); background: var(--green-dim); }
  .tag.amber { border-color: #d9770655; color: var(--amber); background: var(--amber-dim); }
  .tag.red   { border-color: #ef444455; color: var(--red);   background: var(--red-dim);   }

  .dropzone {
    border: 1.5px dashed var(--border);
    border-radius: 10px;
    padding: 28px;
    text-align: center;
    cursor: pointer;
    transition: all 0.2s;
    position: relative;
    margin-bottom: 14px;
  }
  .dropzone:hover, .dropzone.dragover {
    border-color: var(--blue);
    background: var(--blue-dim);
  }
  .dropzone input[type=file] {
    position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; height: 100%;
  }
  .dropzone-icon { font-size: 28px; margin-bottom: 8px; }
  .dropzone-filename {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--green);
    margin-top: 6px;
    display: none;
  }
  .dropzone-text { font-size: 13px; color: var(--muted); line-height: 1.6; }
  .dropzone-text strong { color: var(--blue); }

  .btn {
    width: 100%;
    padding: 12px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--text);
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
  }
  .btn:hover { background: #252a35; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn.green {
    background: var(--green-dim);
    border-color: #16a34a55;
    color: var(--green);
  }
  .btn.green:hover:not(:disabled) { background: #16a34a33; }
  .btn.amber {
    background: var(--amber-dim);
    border-color: #d9770655;
    color: var(--amber);
  }
  .btn.amber:hover:not(:disabled) { background: #d9770633; }

  .results {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 16px;
    display: none;
  }
  .results.show { display: block; }
  .results-title {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--muted);
    letter-spacing: 0.1em;
    text-transform: uppercase;
    margin-bottom: 12px;
  }
  .result-line {
    font-family: var(--mono);
    font-size: 12px;
    line-height: 2;
    color: var(--text);
  }
  .result-line.error { color: var(--red); }
  .result-line.warn  { color: var(--amber); }

  .spinner {
    width: 14px; height: 14px;
    border: 2px solid currentColor;
    border-top-color: transparent;
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
    display: none;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .container.wide { max-width: 680px; }
  .ni-status { font-family: var(--mono); font-size: 12px; color: var(--muted); margin-bottom: 14px; line-height: 1.7; }
  .ni-status b { color: var(--text); font-weight: 500; }
  .progress { height: 6px; background: var(--border); border-radius: 3px; overflow: hidden; margin-bottom: 14px; display: none; }
  .progress > div { height: 100%; width: 0; background: var(--blue); transition: width .4s; }
  .btn-row { display: flex; gap: 8px; margin-bottom: 10px; }
  .btn-row .btn { flex: 1; }
  .link { color: var(--blue); font-size: 12px; font-family: var(--mono); text-decoration: none; }
  .link:hover { text-decoration: underline; }
  .ni-tools { display: none; align-items: center; justify-content: space-between; gap: 8px; margin: 14px 0 8px; font-size: 12px; color: var(--muted); flex-wrap: wrap; }
  .ni-tools label { display: flex; gap: 6px; align-items: center; cursor: pointer; }
  .ni-table { display: none; max-height: 520px; overflow: auto; border: 1px solid var(--border); border-radius: 8px; margin-bottom: 14px; }
  .ni-table table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .ni-table th { position: sticky; top: 0; background: var(--surface); text-align: left; padding: 8px; font-family: var(--mono); font-weight: 500; color: var(--muted); border-bottom: 1px solid var(--border); }
  .ni-table td { padding: 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
  .ni-table tr.off td { opacity: .45; }
  .ni-table img { width: 48px; height: 48px; object-fit: contain; background: #fff; border-radius: 4px; }
  .ni-name { font-weight: 500; }
  .ni-sub { color: var(--muted); font-size: 11px; margin-top: 3px; font-family: var(--mono); }
  .ni-flag { color: var(--amber); }
  .ni-bad { color: var(--red); }
  .ni-ok { color: var(--green); }
  .ni-price { font-family: var(--mono); white-space: nowrap; }
</style>
</head>
<body>
<div class="container">

  <header>
    <div class="logo">🌾</div>
    <div>
      <h1>DVF → SQUARE SYNC</h1>
      <p>Dutch Valley Foods catalog automation</p>
    </div>
  </header>

  <!-- Results -->
  <div class="results" id="results">
    <div class="results-title">Results</div>
    <div id="resultLines"></div>
  </div>

  <!-- Out of Stock -->
  <div class="card">
    <div class="card-title">⚡ Out of Stock</div>
    <div class="card-desc">
      Upload your vendor out-of-stock report. Items on the list will be marked
      <strong style="color:var(--red)">Sellable = N</strong> in Square.
      Items no longer on the list are automatically reset to
      <strong style="color:var(--green)">Sellable = Y</strong>.
    </div>
    <div class="tags">
      <span class="tag red">Sellable = N</span>
      <span class="tag green">Auto-reset</span>
      <span class="tag">out_of_stock*.xls</span>
    </div>
    <div class="dropzone" id="dz-oos">
      <input type="file" accept=".xls,.xlsx" onchange="fileSelected(this, 'oos')">
      <div class="dropzone-icon">📂</div>
      <div class="dropzone-text"><strong>Drop file here</strong> or click to browse</div>
      <div class="dropzone-filename" id="fn-oos"></div>
    </div>
    <button class="btn green" id="btn-oos" onclick="runSync('oos')" disabled>
      <div class="spinner" id="spin-oos"></div>
      <span id="btn-oos-label">▶ Run Out of Stock Sync</span>
    </button>
  </div>

  <!-- Price Update -->
  <div class="card">
    <div class="card-title">💰 Price Update</div>
    <div class="card-desc">
      Upload your DVF wholesale price list. Retail prices are calculated as
      <strong style="color:var(--blue)">Case Price ÷ (1 - GP%)</strong>
      and uploaded to Square for every matching SKU.
    </div>
    <div class="tags">
      <span class="tag blue" style="border-color:#3b82f655;color:var(--blue);background:var(--blue-dim)">Retail = Case ÷ (1 - GP%)</span>
      <span class="tag green">Updates prices</span>
    </div>
    <div class="dropzone" id="dz-price">
      <input type="file" accept=".xls,.xlsx" onchange="fileSelected(this, 'price')">
      <div class="dropzone-icon">💲</div>
      <div class="dropzone-text"><strong>Drop file here</strong> or click to browse</div>
      <div class="dropzone-filename" id="fn-price"></div>
    </div>
    <button class="btn" id="btn-price" onclick="runPriceSync()" disabled
            style="background:var(--blue-dim);border-color:#3b82f655;color:var(--blue)">
      <div class="spinner" id="spin-price"></div>
      <span id="btn-price-label">▶ Run Price Update</span>
    </button>
  </div>

  <!-- New Items -->
  <div class="card">
    <div class="card-title">🆕 New Items</div>
    <div class="card-desc">
      SKUs from the last Price Update that aren't in Square. Look them up on
      dutchvalleyfoods.com, review what was found, then create the ones you want.
      Prices use <strong style="color:var(--blue)">Case Price ÷ (1 - GP%)</strong>.
    </div>
    <div class="tags">
      <span class="tag green">Creates items</span>
      <span class="tag">Name · description · image · category</span>
    </div>
    <div class="ni-status" id="ni-status">Loading…</div>
    <div class="progress" id="ni-progress"><div></div></div>
    <div class="btn-row">
      <button class="btn" id="ni-lookup" onclick="niLookup()" disabled
              style="background:var(--blue-dim);border-color:#3b82f655;color:var(--blue)">🔎 Look Up on DVF</button>
      <a class="btn" id="ni-csv" href="/api/newitems/missing.csv" style="text-decoration:none">⬇ SKU List (CSV)</a>
    </div>
    <div class="btn-row">
      <button class="btn amber" id="ni-retry" onclick="niRetry()" style="display:none">↻ Retry Not Found</button>
      <button class="btn amber" id="ni-fiximg" onclick="niFixImages()" style="display:none">🖼 Add Missing Images</button>
    </div>
    <div class="btn-row" style="align-items:stretch">
      <input id="ni-fixskus" placeholder="Existing SKUs to fix, e.g. 100120, 633006"
             style="flex:2;padding:10px;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-family:var(--mono);font-size:12px">
    </div>
    <div class="btn-row">
      <button class="btn" onclick="niFixSkus()">🖼 Add Images</button>
      <button class="btn" onclick="niFixCats()">📁 Fix Categories</button>
    </div>
    <div class="btn-row">
      <button class="btn" onclick="niWeights()">⚖️ Make Weight Import File</button>
      <a class="btn green" id="ni-wfile" href="/api/newitems/weights.xlsx" style="display:none;text-decoration:none">⬇ Download Weight File</a>
    </div>
    <div class="ni-status" style="font-size:11px;margin-top:-4px">
      Square's API can't set shipping weight. This makes a file for just the new items
      (or the SKUs typed above) — import it in Square Dashboard: Items → Item library → Actions → Import library.
    </div>
    <div class="ni-tools" id="ni-tools">
      <label><input type="checkbox" id="ni-all" onchange="niToggleAll(this.checked)"> Select all</label>
      <span id="ni-count"></span>
    </div>
    <div class="ni-table" id="ni-table">
      <table>
        <thead><tr><th></th><th></th><th>Item</th><th>Price</th></tr></thead>
        <tbody id="ni-rows"></tbody>
      </table>
    </div>
    <button class="btn green" id="ni-create" onclick="niCreate()" style="display:none" disabled>
      ✅ Create Selected in Square
    </button>
  </div>

  <!-- Discontinued -->
  <div class="card">
    <div class="card-title">🗃 Discontinued</div>
    <div class="card-desc">
      Upload your vendor discontinued items report. Matching items will be
      <strong style="color:var(--amber)">archived</strong> in Square —
      hidden from POS and online but recoverable.
    </div>
    <div class="tags">
      <span class="tag amber">Archived</span>
      <span class="tag">discontinued*.xls</span>
    </div>
    <div class="dropzone" id="dz-disc">
      <input type="file" accept=".xls,.xlsx" onchange="fileSelected(this, 'disc')">
      <div class="dropzone-icon">📂</div>
      <div class="dropzone-text"><strong>Drop file here</strong> or click to browse</div>
      <div class="dropzone-filename" id="fn-disc"></div>
    </div>
    <button class="btn amber" id="btn-disc" onclick="runSync('disc')" disabled>
      <div class="spinner" id="spin-disc"></div>
      <span id="btn-disc-label">▶ Run Discontinued Sync</span>
    </button>
  </div>

</div>

<script>
  const files = {};

  function fileSelected(input, type) {
    const file = input.files[0];
    if (!file) return;
    files[type] = file;
    document.getElementById('fn-' + type).style.display = 'block';
    document.getElementById('fn-' + type).textContent = '📄 ' + file.name;
    document.getElementById('btn-' + type).disabled = false;

    // Drag highlight
    const dz = document.getElementById('dz-' + type);
    dz.style.borderColor = 'var(--green)';
    dz.style.background  = 'var(--green-dim)';
  }

  async function runSync(type) {
    const file = files[type];
    if (!file) return;

    const syncType = type === 'oos' ? 'out_of_stock' : 'discontinued';
    const btn      = document.getElementById('btn-' + type);
    const label    = document.getElementById('btn-' + type + '-label');
    const spin     = document.getElementById('spin-' + type);

    btn.disabled      = true;
    spin.style.display = 'block';
    label.textContent  = 'Running...';

    showResults([]);

    const formData = new FormData();
    formData.append('file', file);
    formData.append('type', syncType);

    try {
      const r    = await fetch('/api/sync', {method: 'POST', body: formData});
      const data = await r.json();
      showResults(data.messages || [], data.status === 'error');
    } catch(e) {
      showResults(['❌ Network error: ' + e.message], true);
    } finally {
      btn.disabled       = false;
      spin.style.display = 'none';
      label.textContent  = type === 'oos' ? '▶ Run Out of Stock Sync' : '▶ Run Discontinued Sync';
    }
  }

  async function runPriceSync() {
    const file = files['price'];
    if (!file) return;

    const btn   = document.getElementById('btn-price');
    const label = document.getElementById('btn-price-label');
    const spin  = document.getElementById('spin-price');

    btn.disabled       = true;
    spin.style.display = 'block';
    label.textContent  = 'Running...';
    showResults([]);

    const formData = new FormData();
    formData.append('file', file);

    try {
      const r    = await fetch('/api/sync/prices', {method: 'POST', body: formData});
      const data = await r.json();
      showResults(data.messages || [], data.status === 'error');
      niRefresh();
    } catch(e) {
      showResults(['❌ Network error: ' + e.message], true);
    } finally {
      btn.disabled       = false;
      spin.style.display = 'none';
      label.textContent  = '▶ Run Price Update';
    }
  }

  function showResults(messages, isError) {
    const el = document.getElementById('results');
    const lines = document.getElementById('resultLines');
    el.classList.add('show');
    lines.innerHTML = messages.map(m => {
      let cls = '';
      if (m.includes('❌') || m.includes('Error')) cls = 'error';
      else if (m.includes('⚠️')) cls = 'warn';
      return `<div class="result-line ${cls}">${m}</div>`;
    }).join('');
    el.scrollIntoView({behavior: 'smooth'});
  }

  // Drag and drop (registered once, for all three upload boxes)
  ['oos', 'price', 'disc'].forEach(type => {
    const dz = document.getElementById('dz-' + type);
    dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('dragover'); });
    dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
    dz.addEventListener('drop', e => {
      e.preventDefault();
      dz.classList.remove('dragover');
      const file = e.dataTransfer.files[0];
      if (file) {
        files[type] = file;
        document.getElementById('fn-' + type).style.display = 'block';
        document.getElementById('fn-' + type).textContent = '📄 ' + file.name;
        document.getElementById('btn-' + type).disabled = false;
      }
    });
  });

  // ---------------- New Items ----------------
  let niState = null, niTimer = null;
  const niSel = new Set();
  const esc = t => String(t ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  async function niRefresh() {
    try {
      const r = await fetch('/api/newitems/state');
      niState = await r.json();
    } catch (e) { return; }
    niRender();
    const busy = ['lookup', 'create'].includes(niState.job.phase);
    clearTimeout(niTimer);
    if (busy) niTimer = setTimeout(niRefresh, 2000);
  }

  function niRender() {
    const s = niState, j = s.job;
    const busy = ['lookup', 'create'].includes(j.phase);
    const st = document.getElementById('ni-status');
    let html = s.missing_count
      ? `<b>${s.missing_count}</b> SKU(s) not in Square from the price update at ${esc(s.missing_at)}.`
      : 'No new SKUs yet. Run a Price Update first.';
    if (j.phase === 'lookup') html += `<br>Looking up on DVF… <b>${j.done} / ${j.total}</b>`;
    if (j.phase === 'create') html += `<br>Creating in Square… <b>${j.done} / ${j.total}</b>`;
    if (j.message && !busy) html += `<br>${esc(j.message)}`;
    st.innerHTML = html;

    const pg = document.getElementById('ni-progress');
    pg.style.display = busy ? 'block' : 'none';
    pg.firstElementChild.style.width = j.total ? (100 * j.done / j.total) + '%' : '0';

    document.getElementById('ni-lookup').disabled = busy || !s.missing_count;
    document.getElementById('ni-csv').style.display = s.missing_count ? '' : 'none';
    const wf = document.getElementById('ni-wfile');
    wf.style.display = s.weights_file && !busy ? 'flex' : 'none';
    if (s.weights_file) wf.textContent = `⬇ Download Weight File (${s.weights_file.count} items, ${s.weights_file.at})`;
    const nf = (s.results || []).filter(r => !r.found && !r.square_id).length;
    const rb = document.getElementById('ni-retry');
    rb.style.display = nf && !busy ? 'flex' : 'none';
    rb.textContent = `↻ Retry ${nf} Not Found`;
    const ni = (s.results || []).filter(r => r.square_id && (r.status || '').includes('no image')).length;
    const fb = document.getElementById('ni-fiximg');
    fb.style.display = ni && !busy ? 'flex' : 'none';
    fb.textContent = `🖼 Add ${ni} Missing Image${ni === 1 ? '' : 's'}`;

    const rows = s.results || [];
    const show = rows.length > 0;
    document.getElementById('ni-table').style.display = show ? 'block' : 'none';
    document.getElementById('ni-tools').style.display = show ? 'flex' : 'none';
    document.getElementById('ni-create').style.display = show ? 'flex' : 'none';

    if (j.phase !== 'create' && !niRender.seeded && j.phase === 'review') {
      niSel.clear(); rows.forEach(r => { if (r.selected) niSel.add(r.sku); });
      niRender.seeded = true;
    }
    if (busy) niRender.seeded = false;

    document.getElementById('ni-rows').innerHTML = rows.map(r => {
      const created = r.square_id;
      const on = niSel.has(r.sku) && !created;
      let sub = `SKU ${esc(r.sku)}`;
      if (r.square_category) sub += ` · 📁 ${esc(r.square_category)}`;
      else if (r.breadcrumb) sub += ` · DVF: ${esc(r.breadcrumb)}`;
      else if (r.section) sub += ` · ${esc(r.section)}`;
      let flag = '';
      if (r.status) flag = `<div class="ni-sub ${created ? 'ni-ok' : 'ni-bad'}">${esc(r.status)}</div>`;
      else if (!r.found) flag = `<div class="ni-sub ni-bad">Not found on DVF — ${esc(r.error || '')}. Name is from the price list.</div>`;
      else if (!r.sku_verified) flag = `<div class="ni-sub ni-flag">Check: page didn't clearly show this SKU</div>`;
      const desc = r.description ? `<div class="ni-sub" style="font-family:var(--sans)">${esc(r.description.slice(0, 140))}${r.description.length > 140 ? '…' : ''}</div>` : '';
      const link = r.found ? ` · <a class="link" href="${esc(r.final_url || r.url)}" target="_blank">DVF ↗</a>` : '';
      return `<tr class="${on || created ? '' : 'off'}">
        <td><input type="checkbox" ${on ? 'checked' : ''} ${created || busy ? 'disabled' : ''} onchange="niPick('${esc(r.sku)}', this.checked)"></td>
        <td>${r.image ? `<img src="${esc(r.image)}" loading="lazy" alt="">` : ''}</td>
        <td><div class="ni-name">${esc(r.name || r.list_desc)}</div><div class="ni-sub">${sub}${link}</div>${desc}${flag}</td>
        <td class="ni-price">${r.retail ? '$' + Number(r.retail).toFixed(2) : '<span class="ni-bad">—</span>'}<div class="ni-sub">case $${esc(r.case_price)} · ${esc(r.gp_pct)}</div></td>
      </tr>`;
    }).join('');

    const avail = rows.filter(r => !r.square_id).length;
    const n = [...niSel].filter(k => rows.some(r => r.sku === k && !r.square_id)).length;
    document.getElementById('ni-count').textContent = `${n} of ${avail} selected`;
    document.getElementById('ni-all').checked = avail > 0 && n === avail;
    document.getElementById('ni-create').disabled = busy || n === 0;
  }

  function niPick(sku, on) { on ? niSel.add(sku) : niSel.delete(sku); niRender(); }
  function niToggleAll(on) {
    (niState.results || []).forEach(r => { if (!r.square_id) on ? niSel.add(r.sku) : niSel.delete(r.sku); });
    niRender();
  }

  async function niLookup() {
    niRender.seeded = false;
    const r = await fetch('/api/newitems/lookup', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    const d = await r.json().catch(() => ({message: 'Server error'}));
    if (d.status !== 'ok') alert(d.message);
    niRefresh();
  }

  async function niRetry() {
    const r = await fetch('/api/newitems/retry', {method: 'POST'});
    const d = await r.json().catch(() => ({message: 'Server error'}));
    if (d.status !== 'ok') alert(d.message);
    niRefresh();
  }

  async function niFixSkus() {
    const v = document.getElementById('ni-fixskus').value.trim();
    if (!v) return;
    const r = await fetch('/api/newitems/fix_images', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({skus: v})});
    const d = await r.json().catch(() => ({message: 'Server error'}));
    if (d.status !== 'ok') alert(d.message);
    niRefresh();
  }

  async function niFixCats() {
    const v = document.getElementById('ni-fixskus').value.trim();
    if (!v) return;
    if (!confirm('Move these items to the category matching DVF (creating subcategories if needed)?')) return;
    const r = await fetch('/api/newitems/fix_categories', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({skus: v})});
    const d = await r.json().catch(() => ({message: 'Server error'}));
    if (d.status !== 'ok') alert(d.message);
    niRefresh();
  }

  async function niWeights() {
    const v = document.getElementById('ni-fixskus').value.trim();
    const r = await fetch('/api/newitems/weights', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({skus: v})});
    const d = await r.json().catch(() => ({message: 'Server error'}));
    if (d.status !== 'ok') alert(d.message);
    niRefresh();
  }

  async function niFixImages() {
    const r = await fetch('/api/newitems/fix_images', {method: 'POST'});
    const d = await r.json().catch(() => ({message: 'Server error'}));
    if (d.status !== 'ok') alert(d.message);
    niRefresh();
  }

  async function niCreate() {
    const rows = niState.results || [];
    const skus = [...niSel].filter(k => rows.some(r => r.sku === k && !r.square_id));
    if (!skus.length) return;
    if (!confirm(`Create ${skus.length} new item(s) in Square? They will be sellable immediately.`)) return;
    const r = await fetch('/api/newitems/create', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({skus})});
    const d = await r.json().catch(() => ({message: 'Server error'}));
    if (d.status !== 'ok') alert(d.message);
    niRefresh();
  }

  niRefresh();
</script>
</body>
</html>'''

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
