# Metrics Tracker

Idk I wanted something that fits my exact needs so i vibe coded this.

Self-hosted daily logger for weight, calories, sleep, and training. Simple web UI with charts — works on PC, Mac, and phone browser.

## Quick start (Docker / Proxmox)

```bash
docker compose up -d --build
```

Open `http://<your-server>:8000`.

Optional password (any username, this password):

```bash
echo 'PASSWORD=changeme' > .env
docker compose up -d --build
```

Data is stored in `./data/tracker.db`.

## Local dev

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
set DATA_DIR=./data
python app.py
```

## Usage

1. Pick a date, enter whatever you have (weight, kcal, macros, sleep, training notes).
2. Save — charts and averages update for the selected range (7d / 30d / 90d / 1y).
3. Export CSV from the link under the form.
4. On phone: open the URL → Share / browser menu → **Add to Home Screen**.

Put it behind Tailscale or a reverse proxy if reachable from the internet.
