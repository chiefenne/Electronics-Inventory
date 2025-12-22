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

Container label QR codes are generated using `BASE_URL`.

- Default is `http://localhost:8001`.
- To generate phone-reachable QR codes, run with e.g.:

```bash
BASE_URL="http://<your-lan-ip>:8001" uvicorn app:app --host 0.0.0.0 --port 8001
```

## Notes

- This is intended for trusted networks: there is no authentication/authorization.

## License

MIT — see [LICENSE](LICENSE).
