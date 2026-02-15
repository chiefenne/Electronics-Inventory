# db.py
from __future__ import annotations

import sqlite3
from pathlib import Path
import uuid

DB_PATH = Path(__file__).with_name("inventory.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        # Core data table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS parts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid         TEXT,
                category     TEXT NOT NULL,
                subcategory  TEXT,
                description  TEXT NOT NULL,
                package      TEXT,
                container_id TEXT,
                quantity     INTEGER NOT NULL DEFAULT 0,
                stock_ok_min INTEGER,
                stock_warn_min INTEGER,
                notes        TEXT,
                image_url    TEXT,
                datasheet_url TEXT,
                pinout_url    TEXT,
                pinout_image_url TEXT,
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_parts_category ON parts(category);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_parts_container ON parts(container_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_parts_desc ON parts(description);")

        # Lookup tables (used by dropdowns)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS containers (
                code TEXT PRIMARY KEY,
                name TEXT,
                is_full INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS categories (
                name TEXT PRIMARY KEY
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subcategories (
                name TEXT PRIMARY KEY
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS packages (
                name TEXT PRIMARY KEY
            );
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT PRIMARY KEY,
                username   TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);")

        # Trash table for delete/restore
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS parts_trash (
                trash_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid         TEXT NOT NULL UNIQUE,
                original_id  INTEGER,
                batch_id     TEXT,
                deleted_at   INTEGER NOT NULL,
                deleted_by   TEXT,

                category     TEXT,
                subcategory  TEXT,
                description  TEXT,
                package      TEXT,
                container_id TEXT,
                quantity     INTEGER,
                stock_ok_min INTEGER,
                stock_warn_min INTEGER,
                notes        TEXT,
                image_url    TEXT,
                datasheet_url TEXT,
                pinout_url    TEXT,
                pinout_image_url TEXT,
                created_at   TEXT,
                updated_at   TEXT
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_parts_trash_deleted_at ON parts_trash(deleted_at);")

        # ---- Migrations for existing databases ----
        def _has_column(table: str, column: str) -> bool:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            return any(r[1] == column for r in rows)  # (cid, name, type, notnull, dflt_value, pk)

        # Add missing columns (SQLite supports ADD COLUMN only)
        for col_def in (
            "uuid TEXT",
            "image_url TEXT",
            "datasheet_url TEXT",
            "pinout_url TEXT",
            "pinout_image_url TEXT",
            "created_at TEXT",
            "stock_ok_min INTEGER",
            "stock_warn_min INTEGER",
        ):
            col_name = col_def.split()[0]
            if not _has_column("parts", col_name):
                conn.execute(f"ALTER TABLE parts ADD COLUMN {col_def};")

        # Backfill created_at for existing rows (best effort)
        if _has_column("parts", "created_at"):
            conn.execute(
                """
                UPDATE parts
                SET created_at = updated_at
                WHERE created_at IS NULL OR TRIM(created_at) = ''
                """
            )

        for col_def in (
            "image_url TEXT",
            "pinout_image_url TEXT",
            "created_at TEXT",
            "stock_ok_min INTEGER",
            "stock_warn_min INTEGER",
        ):
            col_name = col_def.split()[0]
            if not _has_column("parts_trash", col_name):
                conn.execute(f"ALTER TABLE parts_trash ADD COLUMN {col_def};")

        # Backfill created_at for existing trash rows (best effort)
        if _has_column("parts_trash", "created_at"):
            conn.execute(
                """
                UPDATE parts_trash
                SET created_at = updated_at
                WHERE created_at IS NULL OR TRIM(created_at) = ''
                """
            )

        # Backfill uuid for existing rows
        missing = conn.execute(
            "SELECT id FROM parts WHERE uuid IS NULL OR TRIM(uuid) = ''"
        ).fetchall()
        for (part_id,) in missing:
            conn.execute(
                "UPDATE parts SET uuid = ? WHERE id = ?",
                (str(uuid.uuid4()), part_id),
            )

        # Backfill pinout_url from the deprecated pinout_image_url if needed
        if _has_column("parts", "pinout_image_url") and _has_column("parts", "pinout_url"):
            conn.execute(
                """
                UPDATE parts
                SET pinout_url = pinout_image_url
                WHERE (pinout_url IS NULL OR TRIM(pinout_url) = '')
                  AND pinout_image_url IS NOT NULL
                  AND TRIM(pinout_image_url) <> ''
                """
            )

        if _has_column("parts_trash", "pinout_image_url") and _has_column("parts_trash", "pinout_url"):
            conn.execute(
                """
                UPDATE parts_trash
                SET pinout_url = pinout_image_url
                WHERE (pinout_url IS NULL OR TRIM(pinout_url) = '')
                  AND pinout_image_url IS NOT NULL
                  AND TRIM(pinout_image_url) <> ''
                """
            )

        # Migration: add is_full column to containers table
        if not _has_column("containers", "is_full"):
            conn.execute("ALTER TABLE containers ADD COLUMN is_full INTEGER NOT NULL DEFAULT 0;")

        # Ensure the unique index exists after backfill
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_parts_uuid ON parts(uuid);")


def list_containers():
    with get_conn() as conn:
        return conn.execute(
            "SELECT code, name, is_full FROM containers ORDER BY code"
        ).fetchall()

def list_categories():
    with get_conn() as conn:
        rows = conn.execute("SELECT name FROM categories ORDER BY name").fetchall()
        return [r["name"] if hasattr(r, "keys") else r[0] for r in rows]

def list_subcategories():
    with get_conn() as conn:
        rows = conn.execute("SELECT name FROM subcategories ORDER BY name").fetchall()
        return [r["name"] if hasattr(r, "keys") else r[0] for r in rows]


def list_packages():
    with get_conn() as conn:
        rows = conn.execute("SELECT name FROM packages ORDER BY name").fetchall()
        return [r["name"] if hasattr(r, "keys") else r[0] for r in rows]

def ensure_container(code: str):
    code = (code or "").strip()
    if not code:
        return
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO containers(code, name) VALUES (?, ?)",
            (code, code),
        )
        conn.commit()

def ensure_category(name: str):
    name = (name or "").strip()
    if not name:
        return
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO categories(name) VALUES (?)",
            (name,),
        )
        conn.commit()

def ensure_subcategory(name: str):
    name = (name or "").strip()
    if not name:
        return
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO subcategories(name) VALUES (?)", (name,))
        conn.commit()


def ensure_package(name: str):
    name = (name or "").strip()
    if not name:
        return
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO packages(name) VALUES (?)", (name,))
        conn.commit()


def get_container_full_map() -> dict:
    """Return {code: bool} mapping for all containers with is_full status."""
    with get_conn() as conn:
        rows = conn.execute("SELECT code, is_full FROM containers").fetchall()
    return {r["code"]: bool(r["is_full"]) for r in rows}


def set_container_full(code: str, is_full: bool) -> None:
    """Set the is_full flag for a container."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE containers SET is_full = ? WHERE code = ?",
            (1 if is_full else 0, code),
        )
        conn.commit()


def fetch_statistics() -> dict:
    """Aggregate inventory statistics for the dashboard."""
    with get_conn() as conn:
        stats: dict = {}

        # --- Totals ---
        row = conn.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(quantity), 0) AS total_qty FROM parts"
        ).fetchone()
        stats["total_parts"] = row["cnt"]
        stats["total_quantity"] = row["total_qty"]

        # Container count (from lookup table)
        row = conn.execute("SELECT COUNT(*) AS cnt FROM containers").fetchone()
        stats["total_containers"] = row["cnt"]

        # Containers marked as full
        row = conn.execute("SELECT COUNT(*) AS cnt FROM containers WHERE is_full = 1").fetchone()
        stats["containers_full"] = row["cnt"]

        # Distinct containers actually in use (assigned to parts)
        row = conn.execute(
            "SELECT COUNT(DISTINCT container_id) AS cnt FROM parts WHERE container_id IS NOT NULL AND TRIM(container_id) <> ''"
        ).fetchone()
        stats["containers_in_use"] = row["cnt"]

        # Out of stock
        row = conn.execute("SELECT COUNT(*) AS cnt FROM parts WHERE quantity = 0").fetchone()
        stats["out_of_stock"] = row["cnt"]

        # Trash count
        row = conn.execute("SELECT COUNT(*) AS cnt FROM parts_trash").fetchone()
        stats["trash_count"] = row["cnt"]

        # --- Parts per category ---
        rows = conn.execute(
            "SELECT category, COUNT(*) AS cnt, COALESCE(SUM(quantity), 0) AS qty "
            "FROM parts GROUP BY category ORDER BY cnt DESC"
        ).fetchall()
        stats["by_category"] = [
            {"name": r["category"], "count": r["cnt"], "quantity": r["qty"]} for r in rows
        ]

        # --- Parts per subcategory (top 15) ---
        rows = conn.execute(
            "SELECT subcategory, COUNT(*) AS cnt, COALESCE(SUM(quantity), 0) AS qty "
            "FROM parts WHERE subcategory IS NOT NULL AND TRIM(subcategory) <> '' "
            "GROUP BY subcategory ORDER BY cnt DESC LIMIT 15"
        ).fetchall()
        stats["by_subcategory"] = [
            {"name": r["subcategory"], "count": r["cnt"], "quantity": r["qty"]} for r in rows
        ]

        # --- Parts per container (top 15) ---
        rows = conn.execute(
            "SELECT container_id, COUNT(*) AS cnt "
            "FROM parts WHERE container_id IS NOT NULL AND TRIM(container_id) <> '' "
            "GROUP BY container_id ORDER BY cnt DESC LIMIT 15"
        ).fetchall()
        stats["by_container"] = [
            {"name": r["container_id"], "count": r["cnt"]} for r in rows
        ]

        # --- Package type distribution ---
        rows = conn.execute(
            "SELECT package, COUNT(*) AS cnt "
            "FROM parts WHERE package IS NOT NULL AND TRIM(package) <> '' "
            "GROUP BY package ORDER BY cnt DESC"
        ).fetchall()
        stats["by_package"] = [
            {"name": r["package"], "count": r["cnt"]} for r in rows
        ]

        # --- Stock status distribution ---
        row = conn.execute("""
            SELECT
                SUM(CASE
                    WHEN quantity = 0 THEN 1 ELSE 0
                END) AS zero,
                SUM(CASE
                    WHEN quantity > 0 AND stock_warn_min IS NOT NULL AND quantity < stock_warn_min THEN 1 ELSE 0
                END) AS low,
                SUM(CASE
                    WHEN quantity > 0
                         AND stock_warn_min IS NOT NULL AND quantity >= stock_warn_min
                         AND stock_ok_min IS NOT NULL AND quantity < stock_ok_min THEN 1 ELSE 0
                END) AS warn,
                SUM(CASE
                    WHEN quantity > 0 AND (stock_ok_min IS NULL OR quantity >= stock_ok_min) THEN 1 ELSE 0
                END) AS ok
            FROM parts
        """).fetchone()
        stats["stock_status"] = {
            "ok": row["ok"] or 0,
            "warn": row["warn"] or 0,
            "low": row["low"] or 0,
            "zero": row["zero"] or 0,
        }

        # --- Data completeness ---
        total = stats["total_parts"] or 1  # avoid div by zero
        completeness = {}
        for field, label in [
            ("image_url", "Image"),
            ("datasheet_url", "Datasheet"),
            ("pinout_url", "Pinout"),
            ("notes", "Notes"),
        ]:
            r = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM parts WHERE {field} IS NOT NULL AND TRIM({field}) <> ''"
            ).fetchone()
            completeness[label] = {"count": r["cnt"], "pct": round(100 * r["cnt"] / total, 1)}
        stats["data_completeness"] = completeness

        # --- Parts added over time (by month) ---
        rows = conn.execute(
            "SELECT SUBSTR(created_at, 1, 7) AS month, COUNT(*) AS cnt "
            "FROM parts WHERE created_at IS NOT NULL AND TRIM(created_at) <> '' "
            "GROUP BY month ORDER BY month"
        ).fetchall()
        cumulative = []
        running = 0
        for r in rows:
            running += r["cnt"]
            cumulative.append({"month": r["month"], "count": r["cnt"], "cumulative": running})
        stats["parts_over_time"] = cumulative

        # --- Recently added (last 10) ---
        rows = conn.execute(
            "SELECT description, category, created_at "
            "FROM parts WHERE created_at IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        stats["recently_added"] = [
            {"description": r["description"], "category": r["category"], "date": r["created_at"]}
            for r in rows
        ]

        # --- Recently updated (last 10) ---
        rows = conn.execute(
            "SELECT description, category, updated_at "
            "FROM parts WHERE updated_at IS NOT NULL "
            "ORDER BY updated_at DESC LIMIT 10"
        ).fetchall()
        stats["recently_updated"] = [
            {"description": r["description"], "category": r["category"], "date": r["updated_at"]}
            for r in rows
        ]

    return stats