# app.py
from __future__ import annotations

import os
import re
import secrets
import time
import ipaddress
from datetime import datetime, timedelta, timezone
import uuid
from pathlib import Path

import csv
import io
import json
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import qrcode
from io import BytesIO
import base64
import httpx

from fastapi import FastAPI, Form, Request, Query
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi import HTTPException

from passlib.hash import pbkdf2_sha256

from jinja2 import Environment, FileSystemLoader, select_autoescape

from db import get_conn, init_db, \
    list_containers, list_categories, list_subcategories, list_packages, \
    ensure_container, ensure_category, ensure_subcategory, ensure_package
from models import PartRequest, PartData, ImageResult, ImageDownloadRequest, DatasheetResult

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional dependency
    OpenAI = None  # type: ignore[assignment]

try:
    from tavily import TavilyClient
except Exception:  # pragma: no cover - optional dependency
    TavilyClient = None  # type: ignore[assignment]

APP_TITLE = "Electronics Inventory"

APP_VERSION = "3.2"

BASE_URL = os.environ.get("INVENTORY_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
print(f"[startup] BASE_URL = {BASE_URL}")

SESSION_COOKIE_NAME = "inventory_session"
SESSION_TTL_SECONDS = 24 * 60 * 60

STATIC_DIR = Path(__file__).with_name("static")

# --- AI Auto-Fill (optional feature) ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()


def _ai_enabled() -> bool:
    return bool(OPENAI_API_KEY and TAVILY_API_KEY and OpenAI and TavilyClient)


def _get_openai_client() -> Optional[OpenAI]:
    if not _ai_enabled():
        return None
    return OpenAI(api_key=OPENAI_API_KEY)


def _get_tavily_client() -> Optional[TavilyClient]:
    if not _ai_enabled():
        return None
    return TavilyClient(api_key=TAVILY_API_KEY)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "y", "on")


def _debug_auth(msg: str) -> None:
    if _env_truthy("INVENTORY_DEBUG_AUTH"):
        print(f"[auth] {msg}")


def _client_ip_from_headers(request: Request) -> str:
    """Best-effort client IP extraction.

    Uses standard proxy headers when present. This is important for the
    home-network auto-login feature when running behind a reverse proxy.
    """

    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        # First IP is the original client.
        ip = xff.split(",", 1)[0].strip()
        if ip:
            return ip

    forwarded = (request.headers.get("forwarded") or "").strip()
    if forwarded:
        # Very small parser for RFC 7239: Forwarded: for=1.2.3.4;proto=https
        m = re.search(r"(?:^|[;,\s])for=(?P<val>\"[^\"]+\"|\[[^\]]+\]|[^;\s,]+)", forwarded, re.IGNORECASE)
        if m:
            val = m.group("val").strip().strip('"')
            if val.startswith("[") and "]" in val:
                val = val[1:val.find("]")]
            if val:
                return val

    return request.client.host if request.client else ""


def _auth_disabled() -> bool:
    # Intended for fully-local setups only. Do NOT use on internet-exposed deployments.
    return _env_truthy("INVENTORY_DISABLE_AUTH")


def _trusted_home_ips() -> set:
    """Return set of IPs/networks from INVENTORY_HOME_IPS env var.

    Comma-separated list of IPs or CIDR ranges that should be treated as "home network".
    Example: INVENTORY_HOME_IPS="80.123.70.54,203.0.113.0/24"
    """
    raw = os.environ.get("INVENTORY_HOME_IPS", "").strip()
    if not raw:
        return set()
    return {ip.strip() for ip in raw.split(",") if ip.strip()}


def _is_home_network(client_ip: str) -> bool:
    """
    Check if the client IP address is from a home/private network.

    This allows skipping password authentication when accessing from your local network,
    without needing to set environment variables.

    Returns True for:
    - 127.0.0.0/8 (localhost, e.g., 127.0.0.1)
    - 10.0.0.0/8 (Class A private network)
    - 172.16.0.0/12 (Class B private network, 172.16-172.31)
    - 192.168.0.0/16 (Class C private network)
    - ::1 (IPv6 localhost)
    - fe80::/10 (IPv6 link-local addresses)
    - Any IP listed in INVENTORY_HOME_IPS env var
    """
    raw = (client_ip or "").strip()
    if not raw:
        return False

    # Be defensive: sometimes values can be comma-separated (e.g. from X-Forwarded-For).
    raw = raw.split(",", 1)[0].strip()

    # Strip IPv6 zone index (e.g. fe80::1%en0)
    raw = raw.split("%", 1)[0].strip()

    # Strip brackets and optional port (e.g. [::1]:1234)
    if raw.startswith("["):
        end = raw.find("]")
        if end != -1:
            raw = raw[1:end]

    # Strip port from IPv4 (e.g. 192.168.1.10:54321)
    if ":" in raw and raw.count(":") == 1 and "." in raw:
        host, maybe_port = raw.rsplit(":", 1)
        if maybe_port.isdigit():
            raw = host

    # Check against explicit whitelist first (supports public IPs behind reverse proxy)
    trusted = _trusted_home_ips()
    if raw in trusted:
        _debug_auth(f"IP {raw} matches INVENTORY_HOME_IPS whitelist")
        return True

    # Check if IP falls within any CIDR range in the whitelist
    try:
        ip_obj = ipaddress.ip_address(raw)
        for entry in trusted:
            if "/" in entry:
                try:
                    if ip_obj in ipaddress.ip_network(entry, strict=False):
                        _debug_auth(f"IP {raw} matches CIDR {entry} in INVENTORY_HOME_IPS")
                        return True
                except ValueError:
                    pass
    except ValueError:
        return False

    return bool(ip_obj.is_loopback or ip_obj.is_private or ip_obj.is_link_local)


def _normalize_pass_hash(auth_pass_hash: str) -> str:
    """Normalize legacy/mis-copied pbkdf2 hashes into passlib format.

    Accepts variants seen in shell/env contexts where '$' is lost or replaced.
    Example legacy value: '-sha256.<salt>.<checksum>'
    """
    raw = (auth_pass_hash or "").strip()
    if raw == "" or raw.startswith("$"):
        return raw

    # Common legacy formats where '$' got replaced with '.'
    m = re.fullmatch(r"(?:pbkdf2)?-?sha256\.(\d+)\.([A-Za-z0-9./]+)\.([A-Za-z0-9./]+)", raw)
    if m:
        rounds, salt, chk = m.group(1), m.group(2), m.group(3)
        return f"$pbkdf2-sha256${rounds}${salt}${chk}"

    # Legacy format missing rounds: '-sha256.<salt>.<checksum>'
    m = re.fullmatch(r"-?sha256\.([A-Za-z0-9./]+)\.([A-Za-z0-9./]+)", raw)
    if m:
        salt, chk = m.group(1), m.group(2)
        rounds = getattr(pbkdf2_sha256, "default_rounds", 29000)
        return f"$pbkdf2-sha256${rounds}${salt}${chk}"

    return raw

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
    # Skip all authentication if globally disabled via environment variable
    if _auth_disabled():
        request.state.user = "local"
        return await call_next(request)

    # Check if the request is coming from the home network
    # If so, automatically authenticate without requiring password
    client_host = _client_ip_from_headers(request)
    _debug_auth(f"middleware path={request.url.path} client_ip={client_host or '<none>'}")
    if client_host and _is_home_network(client_host):
        _debug_auth("home network detected -> allow without session")
        request.state.user = "home_network_user"
        return await call_next(request)

    path = request.url.path

    # Allow unauthenticated access to login page, static files, and favicon
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
    # If authentication is globally disabled, redirect to main page
    if _auth_disabled():
        return RedirectResponse(url="/", status_code=303)

    # If accessing from home network, automatically redirect to main page
    # No password required when on local network
    client_host = _client_ip_from_headers(request)
    _debug_auth(f"GET /login client_ip={client_host or '<none>'}")
    if client_host and _is_home_network(client_host):
        _debug_auth("home network detected -> redirect to /")
        return RedirectResponse(url="/", status_code=303)

    return render("login.html", request=request, title=f"{APP_TITLE} – Login", error="")


@app.post("/login")
def login_post(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
):
    # If authentication is globally disabled, redirect to main page
    if _auth_disabled():
        return RedirectResponse(url="/", status_code=303)

    # If accessing from home network, automatically authenticate
    # without checking credentials
    client_host = _client_ip_from_headers(request)
    _debug_auth(f"POST /login client_ip={client_host or '<none>'}")
    if client_host and _is_home_network(client_host):
        _debug_auth("home network detected -> redirect to /")
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

    # Try pbkdf2 hash verification first; fall back to plaintext comparison for dev setups
    norm_hash = _normalize_pass_hash(auth_pass_hash)
    if norm_hash.startswith("$pbkdf2-sha256$"):
        try:
            pass_ok = pbkdf2_sha256.verify((password or ""), norm_hash)
        except ValueError:
            pass_ok = False
    else:
        # Plaintext fallback (for local dev only – not recommended for production)
        _debug_auth("INVENTORY_PASS_HASH is not a pbkdf2 hash; using plaintext comparison")
        pass_ok = secrets.compare_digest((password or ""), auth_pass_hash)

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
    subcategory: str = "",
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

    if subcategory.strip():
        sql += " AND subcategory = ?"
        params.append(subcategory.strip())

    if container_id.strip():
        sql += " AND container_id = ?"
        params.append(container_id.strip())

    sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
    params.append(limit)

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# --- AI Auto-Fill endpoints (optional feature) ---
EXISTING_CATEGORIES = ["IC", "Module", "Passive", "Semiconductors", "Connectors", "Electromechanical"]
EXISTING_SUBCATS = ["OpAmp", "Logic", "Arduino", "Step-down (buck)", "N-channel", "Timers / Oscillators", "MOSFET"]

# Trusted electronics domains for Tavily search (first pass)
ELECTRONICS_DOMAINS = [
    "digikey.com",
    "mouser.com",
    "reichelt.de",
    "conrad.com",
    "farnell.com",
    "newark.com",
    "lcsc.com",
    "alldatasheet.com",
    "datasheetcatalog.com",
    "ti.com",
    "onsemi.com",
    "st.com",
    "infineon.com",
    "nxp.com",
    "microchip.com",
    "analog.com",
    "vishay.com",
    "tme.eu",
    "electronics.semaf.at",
    "berrybase.at",
    "dfrobot.com",
    "seeedstudio.com",
    "adafruit.com",
    "sparkfun.com",
    "pololu.com",
    "rohm.com",
    "maxim-ic.com",
    "monolithicpower.com",
    "pimoroni.com",
]


@app.post("/api/fill-part", response_model=PartData)
async def fill_part_agent(request: PartRequest) -> PartData:
    if not _ai_enabled():
        raise HTTPException(status_code=503, detail="AI Auto-Fill is disabled")

    tavily = _get_tavily_client()
    client = _get_openai_client()

    if not tavily or not client:
        raise HTTPException(status_code=503, detail="AI Auto-Fill is disabled")

    # 1. TAVILY SEARCH (Text) - first try trusted electronics domains
    search_query = f"{request.query} datasheet specifications"
    results = []
    try:
        response = tavily.search(
            query=search_query,
            search_depth="basic",
            max_results=5,
            include_domains=ELECTRONICS_DOMAINS,
        )
        results = response.get("results", [])
    except Exception:
        results = []

    # Fallback: if fewer than 2 results, retry without domain restriction
    if len(results) < 2:
        try:
            response = tavily.search(
                query=search_query,
                search_depth="basic",
                max_results=5,
            )
            results = response.get("results", [])
        except Exception:
            results = []

    search_context = "\n".join([f"- {r['content']}" for r in results]) if results else "Search failed."

    # 2. ASK OPENAI
    try:
        prompt = f"""
        Analyze the part: "{request.query}" based on the Search Context below.

        Search Context: {search_context}

        --- INSTRUCTIONS ---

        1. DESCRIPTION:
           - Format strictly as: "[Part Name] [Short Generic Type]"
           - Example 1: "IRLZ44N MOSFET"
           - Example 2: "SS34 Schottky"
           - Example 3: "LM358 OpAmp"
           - Keep it extremely short (max 3-4 words). Do NOT include specs here.

        2. NOTES:
           - Extract the MAIN electrical parameters.
           - Format strictly as comma-separated key=value pairs: "Key=Value, Key=Value"
           - Use standard engineering abbreviations.
           - For MOSFETs require: VDss, Id, RdsOn (or Rds).
           - For Diodes require: Vr (Voltage), If (Current).
           - For Regulators/ICs require: Vin, Vout, Iout (or similar).
           - Example output: "VDss=55V, Id=47A, RdsOn=22mOhm"

        3. CATEGORY/SUBCATEGORY:
           - MAP 'Category' strictly to: {json.dumps(EXISTING_CATEGORIES)}
           - MAP 'Subcategory' strictly to: {json.dumps(EXISTING_SUBCATS)} (or 'General').
        """

        completion = client.beta.chat.completions.parse(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a precise electronics inventory assistant."},
                {"role": "user", "content": prompt},
            ],
            response_format=PartData,
        )

        data = completion.choices[0].message.parsed

        # Extract PDF link
        for r in results:
            if ".pdf" in r.get("url", ""):
                data.datasheet_url = r["url"]
                break

        return data

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI Error: {str(e)}")


@app.get("/api/search-images", response_model=List[ImageResult])
async def search_images(query: str, type: str = "part") -> List[ImageResult]:
    if not _ai_enabled():
        raise HTTPException(status_code=503, detail="AI Auto-Fill is disabled")

    tavily = _get_tavily_client()
    if not tavily:
        raise HTTPException(status_code=503, detail="AI Auto-Fill is disabled")

    suffix = " pinout diagram" if type == "pinout" else " electronic component"

    try:
        response = tavily.search(
            query=query + suffix,
            include_images=True,
            include_answer=False,
            max_results=18,  # Fetch more for pagination
        )
        images = response.get("images", [])

        clean_results: List[ImageResult] = []
        for img_url in images:
            if isinstance(img_url, str):
                clean_results.append(ImageResult(title="Image", thumbnail=img_url, url=img_url, source="Web"))
            elif isinstance(img_url, dict):
                clean_results.append(
                    ImageResult(
                        title=img_url.get("description", "Image"),
                        thumbnail=img_url.get("url", ""),
                        url=img_url.get("url", ""),
                        source="Web",
                    )
                )

        return clean_results
    except Exception:
        return []


@app.get("/api/search-datasheet", response_model=List[DatasheetResult])
async def search_datasheet(query: str) -> List[DatasheetResult]:
    """Search for datasheets using Tavily, returning PDF links."""
    if not _ai_enabled():
        raise HTTPException(status_code=503, detail="AI Auto-Fill is disabled")

    tavily = _get_tavily_client()
    if not tavily:
        raise HTTPException(status_code=503, detail="AI Auto-Fill is disabled")

    try:
        # Search for datasheets with PDF focus
        response = tavily.search(
            query=f"{query} datasheet PDF",
            include_answer=False,
            max_results=8,
            include_domains=ELECTRONICS_DOMAINS,
        )

        results: List[DatasheetResult] = []
        seen_urls = set()

        for item in response.get("results", []):
            url = item.get("url", "")
            title = item.get("title", "Datasheet")

            # Skip duplicates
            if url in seen_urls:
                continue
            seen_urls.add(url)

            # Extract domain for source
            try:
                from urllib.parse import urlparse
                domain = urlparse(url).netloc.replace("www.", "")
            except Exception:
                domain = "Web"

            results.append(DatasheetResult(
                title=title[:100],  # Truncate long titles
                url=url,
                source=domain
            ))

        return results
    except Exception:
        return []


@app.post("/api/download-image")
async def download_image(req: ImageDownloadRequest) -> Dict[str, str]:
    """Download an image from URL and save it locally to /static/images or /static/pinouts."""

    # Determine target folder
    if req.type == "pinout":
        target_dir = STATIC_DIR / "pinouts"
        suffix = "_pinout"
    else:
        target_dir = STATIC_DIR / "images"
        suffix = ""

    # Sanitize part description for use as filename
    # Remove special characters, replace spaces with underscores
    safe_name = re.sub(r'[^\w\s-]', '', req.part_description.strip())
    safe_name = re.sub(r'[\s]+', '_', safe_name)
    if not safe_name:
        safe_name = f"image_{secrets.token_hex(4)}"

    # Try to determine file extension from URL
    parsed = urlparse(req.url)
    url_path = parsed.path.lower()

    # Map common extensions
    ext = ".jpg"  # default
    for extension in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"]:
        if url_path.endswith(extension):
            ext = extension
            break

    # Build final filename
    filename = f"{safe_name}{suffix}{ext}"
    filepath = target_dir / filename

    # If file already exists, add a short random suffix
    if filepath.exists():
        filename = f"{safe_name}{suffix}_{secrets.token_hex(3)}{ext}"
        filepath = target_dir / filename

    try:
        # Download the image with httpx (async-friendly)
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get(req.url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; ElectronicsInventory/1.0)"
            })
            response.raise_for_status()

            # Check content type to ensure it's an image
            content_type = response.headers.get("content-type", "").lower()
            if not any(t in content_type for t in ["image/", "application/octet-stream"]):
                raise HTTPException(status_code=400, detail=f"URL did not return an image (got {content_type})")

            # Optionally update extension based on content-type
            if "png" in content_type:
                ext = ".png"
            elif "gif" in content_type:
                ext = ".gif"
            elif "webp" in content_type:
                ext = ".webp"
            elif "svg" in content_type:
                ext = ".svg"
            # Rebuild filename if content-type gave us a better extension
            if not filename.endswith(ext):
                filename = f"{safe_name}{suffix}{ext}"
                filepath = target_dir / filename
                if filepath.exists():
                    filename = f"{safe_name}{suffix}_{secrets.token_hex(3)}{ext}"
                    filepath = target_dir / filename

            # Write the file
            filepath.write_bytes(response.content)

        # Return the local path (relative to static, for use in templates)
        local_path = filename
        return {"filename": local_path, "path": str(filepath)}

    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Failed to download image: HTTP {e.response.status_code}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Failed to download image: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save image: {str(e)}")


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


def list_subcategories_in_use():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT TRIM(subcategory) AS name
            FROM parts
            WHERE subcategory IS NOT NULL AND TRIM(subcategory) <> ''
            ORDER BY name
            """
        ).fetchall()
    return [r["name"] if hasattr(r, "keys") else r[0] for r in rows]


def _maintenance_lists() -> Dict[str, Any]:
    """Return maintenance items with usage counts for each."""
    # Get items in use
    categories_in_use = set(list_categories_in_use())
    subcategories_in_use = set(list_subcategories_in_use())
    containers_in_use = set(list_containers_in_use())
    packages_in_use = set(fetch_distinct("package"))

    # Build lists with usage info: list of {"value": x, "used": bool}
    categories = [
        {"value": c, "used": c in categories_in_use}
        for c in list_categories()
    ]
    subcategories = [
        {"value": s, "used": s in subcategories_in_use}
        for s in list_subcategories()
    ]
    containers_raw = list_containers()
    containers = [
        {
            "value": c["code"] if hasattr(c, "keys") else c[0],
            "used": (c["code"] if hasattr(c, "keys") else c[0]) in containers_in_use,
        }
        for c in containers_raw
    ]
    packages = [
        {"value": p, "used": p in packages_in_use}
        for p in list_packages()
    ]

    # Count unused
    categories_unused = sum(1 for c in categories if not c["used"])
    subcategories_unused = sum(1 for s in subcategories if not s["used"])
    containers_unused = sum(1 for c in containers if not c["used"])
    packages_unused = sum(1 for p in packages if not p["used"])

    return {
        "categories": categories,
        "subcategories": subcategories,
        "containers": containers,
        "packages": packages,
        "categories_unused": categories_unused,
        "subcategories_unused": subcategories_unused,
        "containers_unused": containers_unused,
        "packages_unused": packages_unused,
    }


def _count_usage(field: str, value: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS c FROM parts WHERE {field} = ?",
            (value,),
        ).fetchone()
    if row is None:
        return 0
    return row["c"] if hasattr(row, "keys") else row[0]


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
    subcategory: str = "",
    container_id: str = ""
) -> HTMLResponse:

    parts = fetch_parts(q=q, category=category, subcategory=subcategory, container_id=container_id)

    # IMPORTANT:
    # Search filters must reflect real inventory, not lookup tables
    categories = list_categories_in_use()
    containers = list_containers_in_use()

    # Subcategories for both filter dropdown and datalist suggestions
    subcategories_in_use = list_subcategories_in_use()
    subcategories = list_subcategories() if "list_subcategories" in globals() else []

    return render(
        "index.html",
        request=request,
        title=APP_TITLE,
        parts=parts,
        q=q,
        category=category,
        subcategory=subcategory,
        container_id=container_id,
        categories=categories,
        containers=containers,
        subcategories_in_use=subcategories_in_use,
        subcategories=subcategories,
        ai_enabled=_ai_enabled(),
    )


@app.get("/maintenance", response_class=HTMLResponse)
def maintenance_page(
    request: Request,
    notice: str = "",
    error: str = "",
    notice_section: str = "",
    error_section: str = "",
) -> HTMLResponse:
    for cat in list_categories_in_use():
        ensure_category(cat)
    for sub in list_subcategories_in_use():
        ensure_subcategory(sub)
    for code in list_containers_in_use():
        ensure_container(code)
    for pkg in fetch_distinct("package"):
        ensure_package(pkg)

    data = _maintenance_lists()
    return render(
        "maintenance.html",
        request=request,
        title=f"{APP_TITLE} – Maintenance",
        notice=notice,
        error=error,
        notice_section=notice_section,
        error_section=error_section,
        **data,
    )


@app.post("/maintenance/{entity}/{action}", response_class=HTMLResponse)
def maintenance_action(
    request: Request,
    entity: str,
    action: str,
    value: str = Form(""),
    new_value: str = Form(""),
) -> HTMLResponse:
    entity = (entity or "").strip().lower()
    action = (action or "").strip().lower()

    entities = {
        "category": {"table": "categories", "col": "name", "field": "category"},
        "subcategory": {"table": "subcategories", "col": "name", "field": "subcategory"},
        "container": {"table": "containers", "col": "code", "field": "container_id"},
        "package": {"table": "packages", "col": "name", "field": "package"},
    }

    if entity not in entities or action not in {"add", "rename", "delete"}:
        return HTMLResponse("Invalid maintenance action", status_code=400)

    value = (value or "").strip()
    new_value = (new_value or "").strip()
    meta = entities[entity]

    if action == "add":
        if not value:
            return maintenance_page(request, error="Value is required.", error_section=entity)
        with get_conn() as conn:
            if entity == "container":
                conn.execute(
                    "INSERT OR IGNORE INTO containers(code, name) VALUES (?, ?)",
                    (value, value),
                )
            else:
                conn.execute(
                    f"INSERT OR IGNORE INTO {meta['table']}({meta['col']}) VALUES (?)",
                    (value,),
                )
            conn.commit()
        return maintenance_page(request, notice=f"Added {entity}: {value}", notice_section=entity)

    if action == "rename":
        if not value or not new_value:
            return maintenance_page(request, error="Current and new values are required.", error_section=entity)
        with get_conn() as conn:
            conn.execute(
                f"UPDATE parts SET {meta['field']} = ? WHERE {meta['field']} = ?",
                (new_value, value),
            )
            if entity == "container":
                conn.execute(
                    "INSERT OR IGNORE INTO containers(code, name) VALUES (?, ?)",
                    (new_value, new_value),
                )
                conn.execute("DELETE FROM containers WHERE code = ?", (value,))
            else:
                conn.execute(
                    f"INSERT OR IGNORE INTO {meta['table']}({meta['col']}) VALUES (?)",
                    (new_value,),
                )
                conn.execute(
                    f"DELETE FROM {meta['table']} WHERE {meta['col']} = ?",
                    (value,),
                )
            conn.commit()
        return maintenance_page(request, notice=f"Renamed {entity}: {value} → {new_value}", notice_section=entity)

    if not value:
        return maintenance_page(request, error="Value is required.", error_section=entity)

    used = _count_usage(meta["field"], value)
    if used > 0:
        return maintenance_page(
            request,
            error=f"Cannot delete '{value}' because it is used by {used} part(s).",
            error_section=entity,
        )

    with get_conn() as conn:
        if entity == "container":
            conn.execute("DELETE FROM containers WHERE code = ?", (value,))
        else:
            conn.execute(
                f"DELETE FROM {meta['table']} WHERE {meta['col']} = ?",
                (value,),
            )
        conn.commit()

    return maintenance_page(request, notice=f"Deleted {entity}: {value}", notice_section=entity)


@app.get("/partials/table", response_class=HTMLResponse)
def partial_table(q: str = "", category: str = "", subcategory: str = "", container_id: str = "") -> HTMLResponse:
    parts = fetch_parts(q=q, category=category, subcategory=subcategory, container_id=container_id)
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
    image_url: str = Form(""),
    datasheet_url: str = Form(""),
    pinout_url: str = Form(""),
) -> HTMLResponse:
    category = category.strip()
    description = description.strip()

    # Allow entering just a filename for images
    image_url = _normalize_static_media_path("image_url", image_url)
    pinout_url = _normalize_static_media_path("pinout_url", pinout_url)

    ensure_category(category)
    ensure_container(container_id)
    ensure_subcategory(subcategory)
    ensure_package(package)

    part_uuid = str(uuid.uuid4())

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO parts (
                uuid, category, subcategory, description, package, container_id, quantity, notes,
                image_url, datasheet_url, pinout_url, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
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
                image_url.strip(),
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
    subcategories = list_subcategories()
    return render(
        "_edit_cell.html",
        part=dict(row),
        field=field,
        containers=containers,
        categories=categories,
        subcategories=subcategories,
    )


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
    elif field == "package":
        ensure_package(value)
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


@app.get("/parts/{part_uuid}/view", response_class=HTMLResponse)
def view_part(request: Request, part_uuid: str) -> HTMLResponse:
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

    return render("part_detail.html", part=dict(row), request=request, title=f"{APP_TITLE} – Part Details", ai_enabled=_ai_enabled())


@app.post("/parts/{part_uuid}/detail/quantity", response_class=HTMLResponse)
def save_detail_quantity(
    part_uuid: str,
    quantity: int = Form(...),
    stock_ok_min: str = Form(""),
    stock_warn_min: str = Form(""),
) -> HTMLResponse:
    """Save quantity from detail page - returns updated metadata section"""
    q_int = max(quantity, 0)

    # Parse stock levels
    ok_min: int | None = None
    warn_min: int | None = None

    if stock_ok_min.strip():
        try:
            ok_min = int(stock_ok_min)
        except ValueError:
            pass

    if stock_warn_min.strip():
        try:
            warn_min = int(stock_warn_min)
        except ValueError:
            pass

    with get_conn() as conn:
        conn.execute(
            """
            UPDATE parts
            SET quantity = ?, stock_ok_min = ?, stock_warn_min = ?, updated_at = datetime('now')
            WHERE uuid = ?
            """,
            (q_int, ok_min, warn_min, part_uuid),
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

    # Return just the metadata section for the detail page
    part = dict(row)
    return render("_part_detail_meta.html", part=part)


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
            ("qr_only", "QR only (1 label)"),
            ("text_only", "Text only (1 label)"),
            ("qr_text", "QR + text (1 label)"),
            ("qr_text_2labels", "QR + text (2 labels)"),
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
    mode: str = Form("qr_only"),
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

            raw_qty = (form.get(f"qty_{c}") or "1").strip()
            try:
                qty = int(raw_qty)
            except (TypeError, ValueError):
                qty = 1
            qty = max(1, min(qty, 100))

            label_batch = []

            # QR + text label: QR + optional text on a single label
            if mode == "qr_text":
                label_batch.append({
                    "type": "qr_text",
                    "code": c,
                    "qr": qr_base64(f"{BASE_URL}/containers/{c}"),
                    "text": text
                })

            # QR-only label: container + QR only
            elif mode in ("qr_only", "qr_text_2labels"):
                label_batch.append({
                    "type": "qr_only",
                    "code": c,
                    "qr": qr_base64(f"{BASE_URL}/containers/{c}")
                })
                # Text-only label: container + free text entered in selection UI
                if mode == "qr_text_2labels":
                    label_batch.append({
                        "type": "text_only",
                        "code": c,
                        "text": text
                    })

            # Text-only label only
            elif mode == "text_only":
                label_batch.append({
                    "type": "text_only",
                    "code": c,
                    "text": text
                })

            for _ in range(qty):
                labels.extend(label_batch)

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
