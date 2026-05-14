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
from flask import Flask, request, jsonify, render_template_string

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
                            headers=SQUARE_HEADERS, params=params)
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
                             headers=SQUARE_HEADERS, json=payload)
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
            json={"object_ids": id_list[i:i+1000], "include_related_objects": False}
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
                             headers=SQUARE_HEADERS, json=payload)
        resp.raise_for_status()
    log.info(f"Archived {len(objects)} item(s).")


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------

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

    // Drag and drop
    ['oos', 'disc'].forEach(type => {
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
  }
</script>
</body>
</html>'''

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
