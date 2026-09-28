"""SQLite persistence for download jobs."""
import os
import sqlite3
import threading
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DB_PATH = DATA_DIR / "app.db"

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id             TEXT PRIMARY KEY,
    url            TEXT NOT NULL,
    title          TEXT,
    thumbnail      TEXT,
    format_key     TEXT NOT NULL,
    status         TEXT NOT NULL,
    progress       REAL DEFAULT 0,
    speed          TEXT,
    eta            TEXT,
    filename       TEXT,
    error          TEXT,
    playlist_title TEXT,
    created_at     REAL NOT NULL,
    finished_at    REAL
);
"""


def init() -> None:
    global _conn
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.executescript(SCHEMA)
        _conn.commit()


def upsert(job: dict) -> None:
    cols = ", ".join(job.keys())
    placeholders = ", ".join("?" for _ in job)
    updates = ", ".join(f"{k}=excluded.{k}" for k in job if k != "id")
    with _lock:
        _conn.execute(
            f"INSERT INTO jobs ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            tuple(job.values()),
        )
        _conn.commit()


def all_jobs() -> list[dict]:
    with _lock:
        rows = _conn.execute("SELECT * FROM jobs ORDER BY created_at ASC").fetchall()
    return [dict(r) for r in rows]


def delete(job_id: str) -> None:
    with _lock:
        _conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        _conn.commit()


def delete_finished() -> list[str]:
    with _lock:
        rows = _conn.execute(
            "SELECT id FROM jobs WHERE status IN ('done','error','cancelled')"
        ).fetchall()
        _conn.execute("DELETE FROM jobs WHERE status IN ('done','error','cancelled')")
        _conn.commit()
    return [r["id"] for r in rows]
