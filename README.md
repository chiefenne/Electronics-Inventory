# Electronics Inventory

A lightweight web app to track electronics parts (category, subcategory, description, package, container, quantity, notes) with a simple HTML UI.

## Features

- Add, inline-edit, and delete parts
- Search + filter by category and container
- CSV export
- Container pages and printable container labels with QR codes
- Uses a local SQLite database file (no server required)

## Tech stack

- FastAPI (serves HTML)
- Jinja2 templates
- SQLite (stored in `inventory.db`)

## Quickstart (local)

### 1) Create a virtualenv

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2) Install dependencies

```bash
pip install -r requirements.txt
```

QR code support is included via the `qrcode` dependency in `requirements.txt`.

### 3) Run the server

This app uses HTTP Basic authentication. You must configure credentials via environment variables before it will serve requests.

```bash
uvicorn app:app --reload --host 0.0.0.0 --port 8001
```

Open:

- http://localhost:8001

## Data storage

On startup, the app creates/uses a SQLite database at `inventory.db` (in the repo directory).

- To reset all data: stop the server and delete `inventory.db`.

## Useful routes

- `/` – main inventory table
- `/help` – help page
- `/export.csv` – download CSV export (respects current filters via query params)
- `/containers/{code}` – show parts in a specific container
- `/containers/labels` – printable labels with QR codes

## QR label URLs (important)

Container label QR codes are generated using `BASE_URL` in `app.py`.

- If you run this on a different host/port, update `BASE_URL` so the QR codes point to a reachable URL (e.g. your LAN IP).

## Authentication

This app uses **HTTP Basic Auth** for all routes.

Configure it with:

- `INVENTORY_USER` – username
- `INVENTORY_PASS_HASH` – password hash in Passlib `pbkdf2_sha256` format

Generate a hash (example):

```bash
python3 -c "from passlib.hash import pbkdf2_sha256; print(pbkdf2_sha256.hash('change-me'))"
```

Run with credentials (example):

```bash
export INVENTORY_USER="andreas"
export INVENTORY_PASS_HASH="<paste-hash-here>"
uvicorn app:app --host 0.0.0.0 --port 8001
```

If `INVENTORY_USER` / `INVENTORY_PASS_HASH` are not set, the app returns an error because auth is not configured.

## Notes

- Security: even with Basic Auth, treat this as a trusted-LAN tool. Do not expose it directly to the public internet.
- If you need remote access, prefer a VPN and restrict access.

## License

MIT — see [LICENSE](LICENSE).
