# DVF → Square Sync

Web app for syncing Dutch Valley Foods vendor reports to Square catalog.
Hosted on Render. No local setup required.

## Deploy to Render

1. Push this repo to GitHub
2. Go to https://render.com and create a new **Web Service**
3. Connect your GitHub repo (`catawbacoops/dvf-square-sync`)
4. Render auto-detects `render.yaml` — click **Deploy**
5. In the Render dashboard go to **Environment** and add:
   - `SQUARE_ACCESS_TOKEN` — your Square production access token

## Usage

1. Open your Render app URL
2. Drop your vendor file onto the appropriate card
3. Click Run — results appear instantly

## Files

- `app.py` — Flask app with all sync logic and the UI
- `requirements.txt` — Python dependencies
- `render.yaml` — Render deployment config
