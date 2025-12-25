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
                name TEXT
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

        # Ensure the unique index exists after backfill
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_parts_uuid ON parts(uuid);")


def list_containers():
    with get_conn() as conn:
        return conn.execute(
            "SELECT code, name FROM containers ORDER BY code"
        ).fetchall()

def list_categories():
    with get_conn() as conn:
        rows = conn.execute("SELECT name FROM categories ORDER BY name").fetchall()
        return [r["name"] if hasattr(r, "keys") else r[0] for r in rows]

def list_subcategories():
    with get_conn() as conn:
        rows = conn.execute("SELECT name FROM subcategories ORDER BY name").fetchall()
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
