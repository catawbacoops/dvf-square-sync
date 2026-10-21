"""
catmatch.py
-----------
Match a DVF product's category path to the Square category tree.

DVF:    /spice-seasoning/salt/dendretic/
Square: P-Se > Spice & Seasoning > Salt > Dendritic

Names are compared loosely ("Spice / Seasoning" == "Spice & Seasoning",
"Dendretic" ~ "Dendritic"), and the Square tree may have extra grouping
levels on top (like "P-Se") that DVF doesn't have.
"""

import re
import time
import difflib
import logging

log = logging.getLogger(__name__)

# DVF categories that are tags, not places in the product tree
SKIP_DVF = {"kosher products", "kosher", "featured", "new", "new items", "promotions",
            "preorders", "boost", "gluten free", "organic", "brands", "sale", "clearance",
            "seasonal", "best sellers"}
MAX_EXTRA_ROOTS = 2       # Square levels above the DVF path (e.g. "P-Se")
FUZZY = 0.84              # name similarity needed to count as a match

_cache = {"at": 0, "tree": None}
CACHE_SECONDS = 600


def norm(s):
    s = (s or "").lower().replace("&", " ").replace("/", " ").replace("-", " ").replace("_", " ")
    s = re.sub(r"[^a-z0-9 ]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    # light plural folding: "nuts" ~ "nut", "beans" ~ "bean"
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
                    for w in s.split())


def same(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= FUZZY


# ---------------------------------------------------------------------------
# Square tree
# ---------------------------------------------------------------------------

def load_tree(list_fn, force=False):
    """
    list_fn(types) -> list of catalog objects.
    Returns {"cats": {id: {...}}, "pattern": "leaf" | "chain"}.
    """
    if _cache["tree"] and not force and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["tree"]
    cats = {}
    for o in list_fn("CATEGORY"):
        d = o.get("category_data", {})
        cats[o["id"]] = {
            "id": o["id"],
            "name": d.get("name", ""),
            "parent": (d.get("parent_category") or {}).get("id"),
        }
    for c in cats.values():
        chain, cur, seen = [], c, set()
        while cur and cur["id"] not in seen:
            seen.add(cur["id"])
            chain.append(cur)
            cur = cats.get(cur["parent"])
        chain.reverse()
        c["chain_ids"] = [x["id"] for x in chain]
        c["chain"] = [x["name"] for x in chain]
    children = {}
    for c in cats.values():
        children.setdefault(c["parent"], []).append(c["id"])

    # How do existing items store categories: just the leaf, or the whole chain?
    pattern = "leaf"
    try:
        items = list_fn("ITEM")
        chain_n = leaf_n = 0
        for it in items:
            ids = [x.get("id") for x in it.get("item_data", {}).get("categories", []) if x.get("id")]
            if len(ids) < 2:
                if ids:
                    leaf_n += 1
                continue
            idset = set(ids)
            if any(cats.get(i, {}).get("parent") in idset for i in ids):
                chain_n += 1
            else:
                leaf_n += 1
        pattern = "chain" if chain_n > leaf_n else "leaf"
    except Exception:
        log.exception("Could not read items for category pattern")
        items = []

    tree = {"cats": cats, "children": children, "pattern": pattern, "items": items}
    _cache.update(at=time.time(), tree=tree)
    return tree


# ---------------------------------------------------------------------------
# DVF paths
# ---------------------------------------------------------------------------

def dvf_paths(prod):
    """
    From a storefront-API product, list category paths as lists of level names,
    deepest first, skipping tag-like categories.
    """
    out = []
    for e in (prod.get("categories") or {}).get("edges", []):
        node = e.get("node", {})
        slugs = [s for s in (node.get("path") or "").strip("/").split("/") if s]
        if not slugs:
            continue
        levels = [s.replace("-", " ") for s in slugs]
        if node.get("name"):
            levels[-1] = node["name"]          # real spelling for the leaf
        if norm(levels[0]) in {norm(x) for x in SKIP_DVF} and len(levels) == 1:
            continue
        out.append(levels)
    out.sort(key=len, reverse=True)
    return out


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _match_one(levels, tree):
    """
    Best Square category for one DVF path.
    Returns dict(cat_id, depth matched, extra_roots, missing levels) or None.
    """
    cats, children = tree["cats"], tree["children"]
    best = None
    # Start points: any category whose name matches the DVF top level and
    # sits under at most MAX_EXTRA_ROOTS other levels.
    for c in cats.values():
        if not same(c["name"], levels[0]):
            continue
        extra = len(c["chain"]) - 1
        if extra > MAX_EXTRA_ROOTS:
            continue
        node, depth = c, 1
        while depth < len(levels):
            nxt = next((cats[k] for k in children.get(node["id"], [])
                        if same(cats[k]["name"], levels[depth])), None)
            if not nxt:
                break
            node, depth = nxt, depth + 1
        cand = {"cat_id": node["id"], "depth": depth, "extra_roots": extra,
                "missing": levels[depth:], "dvf": levels}
        key = (depth, -extra)
        if best is None or key > (best["depth"], -best["extra_roots"]):
            best = cand
    return best


def match(prod, tree):
    """Best match across all of a product's DVF category paths."""
    best = None
    for levels in dvf_paths(prod):
        m = _match_one(levels, tree)
        if not m:
            continue
        # Prefer: fully matched, then deepest, then fewer extra roots
        key = (not m["missing"], m["depth"], -m["extra_roots"])
        if best is None or key > (not best["missing"], best["depth"], -best["extra_roots"]):
            best = m
    if best:
        c = tree["cats"][best["cat_id"]]
        best["square_path"] = c["chain"]
        best["category_ids"] = (c["chain_ids"] if tree["pattern"] == "chain" else [c["id"]])
    return best


def item_category_path(sku, tree):
    """The Square category path(s) of an existing item with this SKU."""
    for it in tree.get("items", []):
        for v in it.get("item_data", {}).get("variations", []):
            if v.get("item_variation_data", {}).get("sku", "").strip().upper() == sku:
                ids = [x.get("id") for x in it.get("item_data", {}).get("categories", [])]
                return {"item_id": it["id"], "name": it.get("item_data", {}).get("name"),
                        "category_ids": ids,
                        "paths": [" > ".join(tree["cats"][i]["chain"]) for i in ids if i in tree["cats"]]}
    return None
