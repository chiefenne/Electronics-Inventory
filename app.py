# app.py
from __future__ import annotations

import csv
import io
from typing import Any, Dict, List, Optional

import qrcode
from io import BytesIO
import base64

from fastapi import FastAPI, Form, Request, Query
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from db import get_conn, init_db, \
    list_containers, list_categories, list_subcategories, \
    ensure_container, ensure_category, ensure_subcategory

APP_TITLE = "Electronics Inventory"

BASE_URL = "http://192.168.8.20:8001"

ALLOWED_EDIT_FIELDS = {
    "category",
    "subcategory",
    "description",
    "package",
    "container_id",
    "quantity",
    "notes",
    "datasheet_url",
    "pinout_url",
}

app = FastAPI(title=APP_TITLE)

app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Environment(
    loader=FileSystemLoader("templates"),
    autoescape=select_autoescape(["html", "xml"]),
)


def render(template_name: str, **context: Any) -> HTMLResponse:
    tpl = templates.get_template(template_name)
    return HTMLResponse(tpl.render(**context))


@app.on_event("startup")
def _startup() -> None:
    init_db()

@app.get("/favicon.ico")
async def favicon():
    return FileResponse("static/favicon.ico")

def fetch_parts(
    q: str = "",
    category: str = "",
    container_id: str = "",
    limit: int = 500,
) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM parts WHERE 1=1"
    params: List[Any] = []

    if q.strip():
        sql += " AND (description LIKE ? OR notes LIKE ? OR subcategory LIKE ? OR package LIKE ?)"
        pat = f"%{q.strip()}%"
        params += [pat, pat, pat, pat]

    if category.strip():
        sql += " AND category = ?"
        params.append(category.strip())

    if container_id.strip():
        sql += " AND container_id = ?"
        params.append(container_id.strip())

    sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
    params.append(limit)

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def fetch_distinct(field: str) -> List[str]:
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT {field} AS v FROM parts WHERE {field} IS NOT NULL AND TRIM({field}) <> '' ORDER BY v"
        ).fetchall()
    return [r["v"] for r in rows]



def list_categories_in_use():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT TRIM(category) AS name
            FROM parts
            WHERE category IS NOT NULL AND TRIM(category) <> ''
            ORDER BY name
            """
        ).fetchall()
    return [r["name"] if hasattr(r, "keys") else r[0] for r in rows]


def list_containers_in_use():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT TRIM(container_id) AS code
            FROM parts
            WHERE container_id IS NOT NULL AND TRIM(container_id) <> ''
            ORDER BY code
            """
        ).fetchall()
    return [r["code"] if hasattr(r, "keys") else r[0] for r in rows]



def qr_base64(text: str) -> str:
    img = qrcode.make(text)
    buf = BytesIO()
    img.save(buf, "PNG")   # ← positional argument, not keyword
    return base64.b64encode(buf.getvalue()).decode()



@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    q: str = "",
    category: str = "",
    container_id: str = ""
) -> HTMLResponse:

    parts = fetch_parts(q=q, category=category, container_id=container_id)

    # IMPORTANT:
    # Search filters must reflect real inventory, not lookup tables
    categories = list_categories_in_use()
    containers = list_containers_in_use()

    # Keep subcategories for datalist suggestions if you already had this
    subcategories = list_subcategories() if "list_subcategories" in globals() else []

    return render(
        "index.html",
        request=request,
        title=APP_TITLE,
        parts=parts,
        q=q,
        category=category,
        container_id=container_id,
        categories=categories,
        containers=containers,
        subcategories=subcategories,
    )


@app.get("/partials/table", response_class=HTMLResponse)
def partial_table(q: str = "", category: str = "", container_id: str = "") -> HTMLResponse:
    parts = fetch_parts(q=q, category=category, container_id=container_id)
    return render("_table.html", parts=parts)


@app.post("/parts", response_class=HTMLResponse)
def add_part(
    category: str = Form(...),
    subcategory: str = Form(""),
    description: str = Form(...),
    package: str = Form(""),
    container_id: str = Form(""),
    quantity: int = Form(0),
    notes: str = Form(""),
    datasheet_url: str = Form(""),
    pinout_url: str = Form(""),
) -> HTMLResponse:
    category = category.strip()
    description = description.strip()

    ensure_category(category)
    ensure_container(container_id)
    ensure_subcategory(subcategory)

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO parts (category, subcategory, description, package, container_id, quantity, notes, datasheet_url, pinout_url, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                category,
                subcategory.strip(),
                description,
                package.strip(),
                container_id.strip(),
                max(int(quantity), 0),
                notes.strip(),
                datasheet_url.strip(),
                pinout_url.strip(),
            ),
        )

    # Return updated table (HTMX target)
    parts = fetch_parts()
    return render("_table.html", parts=parts)


@app.post("/parts/{part_id}/delete", response_class=HTMLResponse)
def delete_part(part_id: int) -> HTMLResponse:
    with get_conn() as conn:
        conn.execute("DELETE FROM parts WHERE id = ?", (part_id,))
    parts = fetch_parts()
    return render("_table.html", parts=parts)

@app.get("/parts/{part_id}/edit/{field}", response_class=HTMLResponse)
def edit_cell(part_id: int, field: str) -> HTMLResponse:
    if field not in ALLOWED_EDIT_FIELDS:
        return HTMLResponse("Invalid field", status_code=400)

    with get_conn() as conn:
        row = conn.execute("SELECT * FROM parts WHERE id = ?", (part_id,)).fetchone()

    if row is None:
        return HTMLResponse("Not found", status_code=404)

    containers = list_containers()
    categories = list_categories()
    return render("_edit_cell.html", part=dict(row), field=field,
                  containers=containers, categories=categories)


@app.post("/parts/{part_id}/edit/{field}", response_class=HTMLResponse)
def save_cell(part_id: int, field: str, value: str = Form("")) -> HTMLResponse:
    if field not in ALLOWED_EDIT_FIELDS:
        return HTMLResponse("Invalid field", status_code=400)

    # Basic normalization
    value = value.strip()
    if field == "quantity":
        try:
            q = int(value) if value != "" else 0
        except ValueError:
            q = 0
        value = str(max(q, 0))
    if field == "container_id":
        ensure_container(value)
    elif field == "category":
        ensure_category(value)
    elif field == "subcategory":
        ensure_subcategory(value)
    elif field in ("datasheet_url", "pinout_url"):
        value = value.strip()


    with get_conn() as conn:
        conn.execute(
            f"UPDATE parts SET {field} = ?, updated_at = datetime('now') WHERE id = ?",
            (value, part_id),
        )
        row = conn.execute("SELECT * FROM parts WHERE id = ?", (part_id,)).fetchone()

    if row is None:
        return HTMLResponse("Not found", status_code=404)

    # Return the rendered row so the table updates cleanly
    return render("_row.html", part=dict(row))


@app.get("/export.csv")
def export_csv(q: str = "", category: str = "", container_id: str = "") -> StreamingResponse:
    parts = fetch_parts(q=q, category=category, container_id=container_id, limit=100000)

    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=["id", "category", "subcategory", "description", "package", "container_id", "quantity", "notes", "updated_at"],
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(parts)
    buf.seek(0)

    headers = {"Content-Disposition": "attachment; filename=inventory_export.csv"}
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers=headers)


@app.get("/containers/labels", response_class=HTMLResponse)
def container_labels(request: Request) -> HTMLResponse:
    containers = list_containers_in_use()

    return render(
        "labels_select.html",
        request=request,
        title=f"{APP_TITLE}",
        containers=containers,
        presets=["3348", "3425", "3666"],
        modes=[
            ("asset", "Asset (QR)"),
            ("content", "Content (text)"),
            ("both", "Both"),
        ],
    )


@app.post("/print/labels", response_class=HTMLResponse)
async def print_labels(
    request: Request,
    preset: str = Form("3348"),
    mode: str = Form("asset"),
    code: list[str] = Form([]),
) -> HTMLResponse:

    if not code:
        return HTMLResponse("No containers selected", status_code=400)

    form = await request.form()

    labels = []
    for c in code:
        # Asset label: container + QR
        if mode in ("asset", "both"):
            labels.append({
                "type": "asset",
                "code": c,
                "qr": qr_base64(f"{BASE_URL}/containers/{c}")
            })

        # Content label: container + free text entered in selection UI
        if mode in ("content", "both"):
            text = (form.get(f"text_{c}") or "").strip()
            labels.append({
                "type": "content",
                "code": c,
                "text": text
            })

    return render(
        "labels_print.html",
        request=request,
        title=f"{APP_TITLE}",
        labels=labels,
        preset=preset,
    )



@app.get("/containers/{code}", response_class=HTMLResponse)
def container_view(request: Request, code: str) -> HTMLResponse:
    parts = fetch_parts(container_id=code)
    return render(
        "container.html",
        request=request,
        title=f"Container {code}",
        code=code,
        parts=parts,
    )


@app.get("/help", response_class=HTMLResponse)
def help_page(request: Request) -> HTMLResponse:
    return render("help.html", request=request, title=f"{APP_TITLE}")
