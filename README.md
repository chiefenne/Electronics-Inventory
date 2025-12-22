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

This app uses a login page + server-side sessions. You must configure credentials via environment variables.

Dev (auto-reload on code changes):

```bash
INVENTORY_USER="andreas" \
INVENTORY_PASS_HASH='$pbkdf2-sha256$...$...$...' \
uvicorn app:app --reload --host 0.0.0.0 --port 8001 --proxy-headers
```

Prod (no reload):

```bash
INVENTORY_USER="andreas" \
INVENTORY_PASS_HASH='$pbkdf2-sha256$...$...$...' \
uvicorn app:app --host 0.0.0.0 --port 8001 --proxy-headers
```

Open:

- <http://localhost:8001>

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

## Label printing layouts

Label printing is implemented as:

- Shared label UI/print styles: `static/labels.css`
- One layout file per Avery preset: `static/avery_<preset>.css` (e.g. `static/avery_3425.css`)

To add a new layout later:

1) Create `static/avery_<new-id>.css`
2) Reload the Labels page — the preset list is discovered automatically from `static/avery_*.css`

### Print calibration (offset + spacing)

Printers often introduce small feed/margin offsets even with “Margins: none”. Each preset CSS supports knobs:

- `--label_offset_x` / `--label_offset_y` (shift the whole sheet; negative moves left/up)
- `--label_gap_x` / `--label_gap_y` (space between labels)

Example (move 5mm left and 1mm up):

```css
--label_offset_x: -5mm;
--label_offset_y: -1mm;
```

### Outlines (calibration vs Avery sheets)

By default, label borders are **not printed** (best for real Avery sheets where tiny alignment offsets can make printed
outlines look off).

For calibration or printing on plain paper (where you want cut guides), enable **Print cut outlines** on the Labels page
before printing *(always shown on screen; printed only when enabled)*.

## Authentication

This app uses a **login page + server-side sessions** (stored in SQLite) for all routes.

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
export INVENTORY_PASS_HASH='<paste-hash-here>'
uvicorn app:app --host 0.0.0.0 --port 8001 --proxy-headers
```

If `INVENTORY_USER` / `INVENTORY_PASS_HASH` are not set, the login page will show an error because auth is not configured.

Note: the Passlib hash contains `$` characters. Use **single quotes** around `INVENTORY_PASS_HASH` (or escape `$`) to avoid shell expansion.

## Notes

- Security: even with Basic Auth, treat this as a trusted-LAN tool. Do not expose it directly to the public internet.
- If you need remote access, prefer a VPN and restrict access.

## License

MIT — see [LICENSE](LICENSE).
