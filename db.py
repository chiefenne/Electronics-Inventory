# db.py
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).with_name("inventory.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS parts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                category     TEXT NOT NULL,
                subcategory  TEXT,
                description  TEXT NOT NULL,
                package      TEXT,
                container_id TEXT,
                quantity     INTEGER NOT NULL DEFAULT 0,
                notes        TEXT,
                updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_parts_category ON parts(category);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_parts_container ON parts(container_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_parts_desc ON parts(description);")


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
