# app.py
from __future__ import annotations

import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
import uuid
from pathlib import Path

import csv
import io
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import qrcode
from io import BytesIO
import base64

from fastapi import FastAPI, Form, Request, Query
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi import HTTPException

from passlib.hash import pbkdf2_sha256

from jinja2 import Environment, FileSystemLoader, select_autoescape

from db import get_conn, init_db, \
    list_containers, list_categories, list_subcategories, \
    ensure_container, ensure_category, ensure_subcategory

APP_TITLE = "Electronics Inventory"

APP_VERSION = "2.2"

BASE_URL = os.environ.get("INVENTORY_BASE_URL", "http://127.0.0.1:8001").rstrip("/")

SESSION_COOKIE_NAME = "inventory_session"
SESSION_TTL_SECONDS = 24 * 60 * 60

STATIC_DIR = Path(__file__).with_name("static")


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "y", "on")


def _auth_disabled() -> bool:
    # Intended for fully-local setups only. Do NOT use on internet-exposed deployments.
    return _env_truthy("INVENTORY_DISABLE_AUTH")

ALLOWED_EDIT_FIELDS = {
    "category",
    "subcategory",
    "description",
    "package",
    "container_id",
    "quantity",
    "notes",
    "image_url",
    "datasheet_url",
    "pinout_url",
}


def _parse_stock_levels(text: str) -> tuple[int | None, int | None]:
    """Parse stock levels input.

    Supported:
    - "" (empty) -> disable thresholds (NULL, NULL)
    - "5" -> (5, 5) (green when >=5, red when <5)
    - "10:5" -> (10, 5) (green >=10, yellow >=5 and <10, red <5)
    """
    raw = (text or "").strip()
    if raw == "":
        return None, None

    if ":" not in raw:
        v = int(raw)
        v = max(v, 0)
        return v, v

    left, right = [p.strip() for p in raw.split(":", 1)]
    if left == "" or right == "":
        raise ValueError("Use the format hi:lo (e.g. 10:5)")

    hi = max(int(left), 0)
    lo = max(int(right), 0)
    if hi < lo:
        raise ValueError("Expected hi >= lo (e.g. 10:5)")

    return hi, lo


def _available_label_presets() -> List[str]:
    static_dir = Path(__file__).with_name("static")
    presets: List[str] = []
    for css_file in static_dir.glob("avery_*.css"):
        name = css_file.stem
        if not name.startswith("avery_"):
            continue
        preset = name[len("avery_"):]
        if preset and re.fullmatch(r"[A-Za-z0-9_-]+", preset):
            presets.append(preset)
    return sorted(set(presets))


def _label_preset_metadata() -> dict:
    """Read metadata from preset CSS files.

    Expects a comment like: /* Meta: columns=2, rows=8, label_size=105x57mm */
    """
    static_dir = Path(__file__).with_name("static")
    metadata = {}
    for css_file in static_dir.glob("avery_*.css"):
        name = css_file.stem
        if not name.startswith("avery_"):
            continue
        preset = name[len("avery_"):]
        if not (preset and re.fullmatch(r"[A-Za-z0-9_-]+", preset)):
            continue
        try:
            content = css_file.read_text()
            match = re.search(r'/\*\s*Meta:\s*(.+?)\s*\*/', content)
            if match:
                meta_str = match.group(1)
                meta = {}
                for part in meta_str.split(","):
                    if "=" in part:
                        k, v = part.strip().split("=", 1)
                        meta[k.strip()] = v.strip()
                metadata[preset] = meta
        except Exception:
            pass
    return metadata


def _normalize_static_media_path(field: str, value: str) -> str:
    """Normalize user-entered media references.

    Allows entering just a filename for the per-part image/pinout fields.

    Examples:
    - image_url: "LM358_board.jpg" -> "/static/images/LM358_board.jpg"
    - pinout_url: "LM358_pinout.png" -> "/static/pinouts/LM358_pinout.png"

    If an extension is omitted and there is exactly one matching file by stem
    in the target folder, it will be used.

    Absolute paths (starting with '/') and URLs are preserved.
    """
    raw = (value or "").strip()
    if raw == "":
        return ""

    # Leave full URLs or absolute paths untouched
    lowered = raw.lower()
    if "://" in raw or lowered.startswith("data:") or lowered.startswith("mailto:"):
        return raw
    if raw.startswith("/"):
        return raw

    # Normalize a few common relative patterns into /static/…
    for prefix in ("static/", "static\\"):
        if raw.startswith(prefix):
            return "/" + raw.replace("\\", "/")
    for prefix in ("images/", "images\\", "pinouts/", "pinouts\\"):
        if raw.startswith(prefix):
            return "/static/" + raw.replace("\\", "/")

    # Only treat plain filenames (no path separators) as candidates.
    if "/" in raw or "\\" in raw or ".." in raw:
        return raw

    subdir = None
    if field == "image_url":
        subdir = "images"
    elif field == "pinout_url":
        subdir = "pinouts"

    if subdir is None:
        return raw

    folder = STATIC_DIR / subdir
    if not folder.exists() or not folder.is_dir():
        return f"/static/{subdir}/{raw}"

    # 1) Exact match
    if (folder / raw).exists():
        return f"/static/{subdir}/{raw}"

    # 2) Case-insensitive match
    try:
        entries = [p for p in folder.iterdir() if p.is_file()]
    except Exception:
        entries = []

    ci = [p for p in entries if p.name.lower() == raw.lower()]
    if len(ci) == 1:
        return f"/static/{subdir}/{ci[0].name}"

    # 3) Stem match when extension omitted
    if "." not in raw:
        stem_matches = [p for p in entries if p.stem.lower() == raw.lower()]
        if len(stem_matches) == 1:
            return f"/static/{subdir}/{stem_matches[0].name}"

    # Default: assume it belongs in that folder
    return f"/static/{subdir}/{raw}"


def _auth_config() -> tuple[str, str]:
    # Read at request-time so runtime env changes (service env, docker env, etc.) are respected.
    return (
        os.environ.get("INVENTORY_USER", ""),
        os.environ.get("INVENTORY_PASS_HASH", ""),
    )


def _now_ts() -> int:
    return int(time.time())


def _cleanup_expired_sessions(now_ts: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_ts,))


def _get_valid_session(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None

    now_ts = _now_ts()
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_ts,))
        row = conn.execute(
            "SELECT token, username, expires_at FROM sessions WHERE token = ? AND expires_at > ?",
            (token, now_ts),
        ).fetchone()

    return dict(row) if row is not None else None


def _create_session(username: str) -> tuple[str, int]:
    token = secrets.token_urlsafe(32)
    now_ts = _now_ts()
    expires_ts = now_ts + SESSION_TTL_SECONDS

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions(token, username, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, username, now_ts, expires_ts),
        )

    return token, expires_ts


def _delete_session(token: str) -> None:
    if not token:
        return
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


app = FastAPI()


@app.middleware("http")
async def session_auth_middleware(request: Request, call_next):
    if _auth_disabled():
        request.state.user = "local"
        return await call_next(request)

    path = request.url.path

    # Allow unauthenticated access
    if path == "/login" or path == "/favicon.ico" or path.startswith("/static/"):
        return await call_next(request)

    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    session = _get_valid_session(token)
    if session is not None:
        request.state.user = session.get("username")
        return await call_next(request)

    accept = request.headers.get("accept", "")
    wants_html = ("text/html" in accept) or ("*/*" in accept) or (accept.strip() == "")
    if wants_html:
        return RedirectResponse(url="/login", status_code=303)

    return HTMLResponse("Unauthorized", status_code=401)

app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Environment(
    loader=FileSystemLoader("templates"),
    autoescape=select_autoescape(["html", "xml"]),
)

templates.globals["app_title"] = APP_TITLE
templates.globals["app_version"] = APP_VERSION

def render(template_name: str, **context: Any) -> HTMLResponse:
    tpl = templates.get_template(template_name)
    return HTMLResponse(tpl.render(**context))


def render_with_status(template_name: str, status_code: int, **context: Any) -> HTMLResponse:
    tpl = templates.get_template(template_name)
    return HTMLResponse(tpl.render(**context), status_code=status_code)


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request) -> HTMLResponse:
    if _auth_disabled():
        return RedirectResponse(url="/", status_code=303)
    return render("login.html", request=request, title=f"{APP_TITLE} – Login", error="")


@app.post("/login")
def login_post(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
):
    if _auth_disabled():
        return RedirectResponse(url="/", status_code=303)

    auth_user, auth_pass_hash = _auth_config()
    if not auth_user or not auth_pass_hash:
        return render_with_status(
            "login.html",
            500,
            request=request,
            title=f"{APP_TITLE} – Login",
            error="Auth not configured on server (set INVENTORY_USER and INVENTORY_PASS_HASH)",
        )

    user_ok = secrets.compare_digest((username or ""), auth_user)
    pass_ok = pbkdf2_sha256.verify((password or ""), auth_pass_hash)

    if not (user_ok and pass_ok):
        return render(
            "login.html",
            request=request,
            title=f"{APP_TITLE} – Login",
            error="Invalid username or password",
        )

    token, expires_ts = _create_session(username=auth_user)
    resp = RedirectResponse(url="/", status_code=303)
    expires_dt = datetime.fromtimestamp(expires_ts, tz=timezone.utc)
    resp.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        expires=expires_dt,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return resp


@app.get("/logout")
def logout(request: Request):
    if _auth_disabled():
        return RedirectResponse(url="/", status_code=303)
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    _delete_session(token)
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return resp

@app.get("/favicon.ico")
async def favicon():
    return FileResponse("static/favicon.ico")

def fetch_parts(
    q: str = "",
    category: str = "",
    container_id: str = "",
    limit: int = 500,
) -> List[Dict[str, Any]]:
    sql = (
        "SELECT *, "
        "datetime(created_at, 'localtime') AS created_at_local, "
        "datetime(updated_at, 'localtime') AS updated_at_local "
        "FROM parts WHERE 1=1"
    )
    params: List[Any] = []

    if q.strip():
        sql += " AND (description LIKE ? OR notes LIKE ? OR subcategory LIKE ? OR package LIKE ? OR container_id LIKE ?)"
        pat = f"%{q.strip()}%"
        params += [pat, pat, pat, pat, pat]

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


def fetch_trash(
    q: str = "",
    category: str = "",
    container_id: str = "",
    limit: int = 500,
) -> List[Dict[str, Any]]:
    sql = "SELECT *, datetime(deleted_at, 'unixepoch', 'localtime') AS deleted_at_human FROM parts_trash WHERE 1=1"
    params: List[Any] = []

    if q.strip():
        sql += " AND (description LIKE ? OR notes LIKE ? OR subcategory LIKE ? OR package LIKE ? OR container_id LIKE ?)"
        pat = f"%{q.strip()}%"
        params += [pat, pat, pat, pat, pat]

    if category.strip():
        sql += " AND category = ?"
        params.append(category.strip())

    if container_id.strip():
        sql += " AND container_id = ?"
        params.append(container_id.strip())

    sql += " ORDER BY deleted_at DESC, trash_id DESC LIMIT ?"
    params.append(limit)

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _trash_parts(where_sql: str, params: List[Any], deleted_by: str) -> str:
    batch_id = secrets.token_urlsafe(12)
    now_ts = _now_ts()

    with get_conn() as conn:
        conn.execute("BEGIN")
        # Copy rows into trash
        conn.execute(
            f"""
            INSERT INTO parts_trash(
                uuid, original_id, batch_id, deleted_at, deleted_by,
                category, subcategory, description, package, container_id, quantity, stock_ok_min, stock_warn_min, notes,
                image_url, datasheet_url, pinout_url, pinout_image_url, created_at, updated_at
            )
            SELECT
                uuid, id, ?, ?, ?,
                category, subcategory, description, package, container_id, quantity, stock_ok_min, stock_warn_min, notes,
                image_url, datasheet_url, pinout_url, pinout_image_url, created_at, updated_at
            FROM parts
            WHERE {where_sql}
            """,
            [batch_id, now_ts, deleted_by, *params],
        )
        # Delete from parts
        conn.execute(
            f"DELETE FROM parts WHERE {where_sql}",
            params,
        )
        conn.execute("COMMIT")

    return batch_id


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

    # Allow entering just a filename for pinouts
    pinout_url = _normalize_static_media_path("pinout_url", pinout_url)

    ensure_category(category)
    ensure_container(container_id)
    ensure_subcategory(subcategory)

    part_uuid = str(uuid.uuid4())

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO parts (
                uuid, category, subcategory, description, package, container_id, quantity, notes,
                datasheet_url, pinout_url, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            (
                part_uuid,
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


@app.post("/parts/{part_uuid}/delete", response_class=HTMLResponse)
def delete_part(request: Request, part_uuid: str) -> HTMLResponse:
    deleted_by = getattr(request.state, "user", "") or ""
    _trash_parts("uuid = ?", [part_uuid], deleted_by=deleted_by)

    # HTMX main-table delete expects the table fragment back.
    if request.headers.get("hx-request", "").lower() == "true":
        referer = request.headers.get("referer", "")

        # If the delete was triggered from a container page, keep that view filtered.
        try:
            ref = urlparse(referer)
            if ref.path.startswith("/containers/") and not ref.path.startswith("/containers/labels"):
                code = ref.path[len("/containers/"):].strip("/")
                if code:
                    parts = fetch_parts(container_id=code)
                    return render("_table.html", parts=parts)
        except Exception:
            pass

        parts = fetch_parts()
        return render("_table.html", parts=parts)

    # Non-HTMX (e.g., container view): redirect back to where the user came from.
    referer = request.headers.get("referer", "")
    dest = "/"
    try:
        ref = urlparse(referer)
        base = urlparse(str(request.base_url))
        if ref.scheme == base.scheme and ref.netloc == base.netloc and ref.path:
            dest = ref.path + (("?" + ref.query) if ref.query else "")
    except Exception:
        dest = "/"

    return RedirectResponse(url=dest, status_code=303)

@app.get("/parts/{part_uuid}/edit/{field}", response_class=HTMLResponse)
def edit_cell(part_uuid: str, field: str) -> HTMLResponse:
    if field not in ALLOWED_EDIT_FIELDS:
        return HTMLResponse("Invalid field", status_code=400)

    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT *,
                   datetime(created_at, 'localtime') AS created_at_local,
                   datetime(updated_at, 'localtime') AS updated_at_local
            FROM parts
            WHERE uuid = ?
            """,
            (part_uuid,),
        ).fetchone()

    if row is None:
        return HTMLResponse("Not found", status_code=404)

    containers = list_containers()
    categories = list_categories()
    return render("_edit_cell.html", part=dict(row), field=field,
                  containers=containers, categories=categories)


@app.post("/parts/{part_uuid}/edit/{field}", response_class=HTMLResponse)
def save_cell(
    part_uuid: str,
    field: str,
    value: str = Form(""),
    stock_levels: str = Form(""),
) -> HTMLResponse:
    if field not in ALLOWED_EDIT_FIELDS:
        return HTMLResponse("Invalid field", status_code=400)

    # Basic normalization
    value = value.strip()

    q_int = 0
    ok_min: int | None = None
    warn_min: int | None = None
    keep_levels = False

    if field == "quantity":
        try:
            q = int(value) if value != "" else 0
        except ValueError:
            q = 0

        q_int = max(q, 0)

        try:
            ok_min, warn_min = _parse_stock_levels(stock_levels)
        except ValueError:
            keep_levels = True
    if field == "container_id":
        ensure_container(value)
    elif field == "category":
        ensure_category(value)
    elif field == "subcategory":
        ensure_subcategory(value)
    elif field in ("datasheet_url", "pinout_url", "image_url"):
        if field in ("pinout_url", "image_url"):
            value = _normalize_static_media_path(field, value)
        else:
            value = value.strip()


    with get_conn() as conn:
        if field == "quantity":
            if keep_levels:
                current = conn.execute(
                    "SELECT stock_ok_min, stock_warn_min FROM parts WHERE uuid = ?",
                    (part_uuid,),
                ).fetchone()
                if current is not None:
                    ok_min = current[0]
                    warn_min = current[1]
                else:
                    ok_min, warn_min = None, None

            conn.execute(
                """
                UPDATE parts
                SET quantity = ?, stock_ok_min = ?, stock_warn_min = ?, updated_at = datetime('now')
                WHERE uuid = ?
                """,
                (q_int, ok_min, warn_min, part_uuid),
            )
        else:
            conn.execute(
                f"UPDATE parts SET {field} = ?, updated_at = datetime('now') WHERE uuid = ?",
                (value, part_uuid),
            )
        row = conn.execute(
            """
            SELECT *,
                   datetime(created_at, 'localtime') AS created_at_local,
                   datetime(updated_at, 'localtime') AS updated_at_local
            FROM parts
            WHERE uuid = ?
            """,
            (part_uuid,),
        ).fetchone()

    if row is None:
        return HTMLResponse("Not found", status_code=404)

    # Return the rendered row so the table updates cleanly
    return render("_row.html", part=dict(row))


@app.get("/parts/{part_uuid}/row", response_class=HTMLResponse)
def get_row(part_uuid: str) -> HTMLResponse:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT *,
                   datetime(created_at, 'localtime') AS created_at_local,
                   datetime(updated_at, 'localtime') AS updated_at_local
            FROM parts
            WHERE uuid = ?
            """,
            (part_uuid,),
        ).fetchone()

    if row is None:
        return HTMLResponse("Not found", status_code=404)

    return render("_row.html", part=dict(row))


@app.post("/parts/{part_uuid}/quantity_delta", response_class=HTMLResponse)
def quantity_delta(part_uuid: str, delta: int = Form(0)) -> HTMLResponse:
    try:
        d = int(delta)
    except Exception:
        d = 0
    # Accept aggregated deltas from the UI (e.g. rapid clicks batched client-side),
    # but clamp to a reasonable range to prevent accidental huge jumps.
    if d > 50:
        d = 50
    elif d < -50:
        d = -50

    with get_conn() as conn:
        row = conn.execute(
            "SELECT quantity FROM parts WHERE uuid = ?",
            (part_uuid,),
        ).fetchone()
        if row is None:
            return HTMLResponse("Not found", status_code=404)

        current_qty = int(row[0] or 0)
        new_qty = max(current_qty + d, 0)
        conn.execute(
            "UPDATE parts SET quantity = ?, updated_at = datetime('now') WHERE uuid = ?",
            (new_qty, part_uuid),
        )

        updated = conn.execute(
            """
            SELECT *,
                   datetime(created_at, 'localtime') AS created_at_local,
                   datetime(updated_at, 'localtime') AS updated_at_local
            FROM parts
            WHERE uuid = ?
            """,
            (part_uuid,),
        ).fetchone()

    if updated is None:
        return HTMLResponse("Not found", status_code=404)

    return render("_row.html", part=dict(updated))


@app.get("/restore", response_class=HTMLResponse)
def restore_page(
    request: Request,
    q: str = "",
    category: str = "",
    container_id: str = "",
) -> HTMLResponse:
    items = fetch_trash(q=q, category=category, container_id=container_id)
    categories = list_categories_in_use()
    containers = list_containers_in_use()
    return render(
        "restore.html",
        request=request,
        title=f"{APP_TITLE}",
        items=items,
        q=q,
        category=category,
        container_id=container_id,
        categories=categories,
        containers=containers,
        error="",
    )


@app.post("/restore", response_class=HTMLResponse)
async def restore_post(
    request: Request,
    action: str = Form("selected"),
    q: str = Form(""),
    category: str = Form(""),
    container_id: str = Form(""),
    uuid: List[str] = Form([]),
) -> HTMLResponse:
    # Determine which trash rows to target
    if action in ("filter", "delete_filter"):
        rows = fetch_trash(q=q, category=category, container_id=container_id, limit=100000)
        target_uuids = [r.get("uuid", "") for r in rows if r.get("uuid")]
    else:
        target_uuids = [u for u in uuid if u]

    if not target_uuids:
        items = fetch_trash(q=q, category=category, container_id=container_id)
        return render(
            "restore.html",
            request=request,
            title=f"{APP_TITLE}",
            items=items,
            q=q,
            category=category,
            container_id=container_id,
            categories=list_categories_in_use(),
            containers=list_containers_in_use(),
            error="Nothing selected",
        )

    # Permanent delete from trash
    if action in ("delete_filter", "delete_selected"):
        with get_conn() as conn:
            placeholders = ",".join(["?"] * len(target_uuids))
            conn.execute(
                f"DELETE FROM parts_trash WHERE uuid IN ({placeholders})",
                target_uuids,
            )
        return RedirectResponse(url="/restore", status_code=303)

    with get_conn() as conn:
        placeholders = ",".join(["?"] * len(target_uuids))

        existing = conn.execute(
            f"SELECT uuid FROM parts WHERE uuid IN ({placeholders})",
            target_uuids,
        ).fetchall()
        if existing:
            items = fetch_trash(q=q, category=category, container_id=container_id)
            return render(
                "restore.html",
                request=request,
                title=f"{APP_TITLE}",
                items=items,
                q=q,
                category=category,
                container_id=container_id,
                categories=list_categories_in_use(),
                containers=list_containers_in_use(),
                error="Some items already exist in inventory and cannot be restored again",
            )

        conn.execute("BEGIN")
        conn.execute(
            f"""
            INSERT INTO parts(
                uuid, category, subcategory, description, package, container_id, quantity, stock_ok_min, stock_warn_min, notes,
                image_url, datasheet_url, pinout_url, pinout_image_url, created_at, updated_at
            )
            SELECT
                uuid, category, subcategory, description, package, container_id, quantity, stock_ok_min, stock_warn_min, notes,
                image_url, datasheet_url, pinout_url, pinout_image_url,
                COALESCE(created_at, updated_at, datetime('now')),
                datetime('now')
            FROM parts_trash
            WHERE uuid IN ({placeholders})
            """,
            target_uuids,
        )
        conn.execute(
            f"DELETE FROM parts_trash WHERE uuid IN ({placeholders})",
            target_uuids,
        )
        conn.execute("COMMIT")

    return RedirectResponse(url="/restore", status_code=303)


@app.get("/export.csv")
def export_csv(q: str = "", category: str = "", container_id: str = "") -> StreamingResponse:
    parts = fetch_parts(q=q, category=category, container_id=container_id, limit=100000)

    buf = io.StringIO()
    fieldnames = [
        "category",
        "subcategory",
        "description",
        "package",
        "container_id",
        "quantity",
        "stock_ok_min",
        "stock_warn_min",
        "notes",
        "image_url",
        "datasheet_url",
        "pinout_url",
        "updated_at",
        "uuid",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")

    writer.writeheader()
    writer.writerows(parts)
    buf.seek(0)

    headers = {"Content-Disposition": "attachment; filename=inventory_export.csv"}
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers=headers)


@app.get("/containers/labels", response_class=HTMLResponse)
def container_labels(request: Request) -> HTMLResponse:
    containers = list_containers_in_use()
    categories = list_categories_in_use()
    presets = _available_label_presets() or ["3348", "3425", "3666"]
    preset_meta = _label_preset_metadata()

    return render(
        "labels_select.html",
        request=request,
        title=f"{APP_TITLE}",
        containers=containers,
        categories=categories,
        presets=presets,
        preset_meta=preset_meta,
        container_modes=[
            ("asset", "Asset (QR only)"),
            ("combo", "Asset + text (combined)"),
            ("content", "Content (text only)"),
            ("both", "Separate labels (QR + text)"),
        ],
        part_modes=[
            ("part", "Part label"),
        ],
    )


@app.get("/labels/parts", response_class=HTMLResponse)
def labels_parts_list(
    request: Request,
    q: str = "",
    category: str = "",
    container_id: str = "",
) -> HTMLResponse:
    """Return filtered parts list for label selection (HTMX partial)."""
    # Only return results if at least one filter is set
    if not q.strip() and not category.strip() and not container_id.strip():
        return HTMLResponse('<div class="muted">Use filters above to find parts</div>')

    parts = fetch_parts(q=q, category=category, container_id=container_id, limit=200)
    return render("_labels_parts_list.html", parts=parts)


@app.post("/print/labels", response_class=HTMLResponse)
async def print_labels(
    request: Request,
    preset: str = Form("3348"),
    mode: str = Form("asset"),
    code: list[str] = Form([]),
    outline: str = Form(""),
    start_position: int = Form(1),
) -> HTMLResponse:

    if preset not in _available_label_presets():
        raise HTTPException(status_code=400, detail="Invalid label preset")

    form = await request.form()

    # Check for part labels
    part_uuids = form.getlist("part_uuid[]")

    if not code and not part_uuids:
        return HTMLResponse("No containers or parts selected", status_code=400)

    # Add blank labels to skip positions (start_position is 1-based)
    labels = []
    skip_count = max(0, start_position - 1)
    for _ in range(skip_count):
        labels.append({"type": "skip"})

    # Part labels
    if part_uuids:
        with get_conn() as conn:
            placeholders = ",".join("?" * len(part_uuids))
            rows = conn.execute(
                f"SELECT * FROM parts WHERE uuid IN ({placeholders})",
                list(part_uuids)
            ).fetchall()
            parts_map = {r["uuid"]: dict(r) for r in rows}

        for uuid in part_uuids:
            part = parts_map.get(uuid)
            if part:
                labels.append({
                    "type": "part",
                    "container": part.get("container_id", ""),
                    "description": part.get("description", ""),
                    "notes": part.get("notes", ""),
                    "subcategory": part.get("subcategory", ""),
                })

    # Container labels
    else:
        for c in code:
            text = (form.get(f"text_{c}") or "").strip()

            # Combo label: QR + optional text on a single label
            if mode == "combo":
                labels.append({
                    "type": "combo",
                    "code": c,
                    "qr": qr_base64(f"{BASE_URL}/containers/{c}"),
                    "text": text
                })

            # Asset label: container + QR only
            elif mode in ("asset", "both"):
                labels.append({
                    "type": "asset",
                    "code": c,
                    "qr": qr_base64(f"{BASE_URL}/containers/{c}")
                })
                # Content label: container + free text entered in selection UI
                if mode == "both":
                    labels.append({
                        "type": "content",
                        "code": c,
                        "text": text
                    })

            # Content label only
            elif mode == "content":
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
        show_outline=bool(outline),
    )

    return render(
        "labels_print.html",
        request=request,
        title=f"{APP_TITLE}",
        labels=labels,
        preset=preset,
        show_outline=bool(outline),
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
