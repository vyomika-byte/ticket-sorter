"""SQLite schema + category table loading. No business rules live here."""
import csv
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("TRIAGE_DB") or ROOT / "triage.db")
CATEGORIES_CSV = ROOT / "categories.csv"

VALID_PRIORITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    category         TEXT PRIMARY KEY,
    description      TEXT NOT NULL,
    default_priority TEXT NOT NULL,
    sla_hours        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id       TEXT,
    channel           TEXT,
    raw_text          TEXT NOT NULL,      -- audit only, never returned by list APIs
    redacted_text     TEXT NOT NULL,
    content_hash      TEXT NOT NULL UNIQUE,
    ingested_at       TEXT NOT NULL,      -- UTC ISO-8601, first-seen time
    category          TEXT REFERENCES categories(category),  -- NULL when needs review
    priority          TEXT NOT NULL,
    sla_hours         INTEGER NOT NULL,
    sla_deadline      TEXT NOT NULL,
    confidence        REAL,
    extracted_json    TEXT NOT NULL,
    summary           TEXT,
    draft_reply       TEXT,
    flags_json        TEXT NOT NULL DEFAULT '[]',
    model_output_json TEXT,
    status            TEXT NOT NULL,      -- PENDING_REVIEW | NEEDS_HUMAN_REVIEW | APPROVED
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_category ON tickets(category);
CREATE INDEX IF NOT EXISTS idx_tickets_status   ON tickets(status);

CREATE TABLE IF NOT EXISTS audit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL REFERENCES tickets(id),
    event_type  TEXT NOT NULL,            -- CREATED | DUPLICATE_SUBMIT | EDITED | APPROVED
    actor       TEXT NOT NULL,
    before_json TEXT,
    after_json  TEXT,
    ts          TEXT NOT NULL
);
"""


def get_conn(path=DB_PATH) -> sqlite3.Connection:
    # One connection per request, used sequentially; FastAPI may hop worker threads between
    # the dependency and the endpoint, so the same-thread check must be off.
    conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer
    return conn


def load_categories(conn: sqlite3.Connection, csv_path=CATEGORIES_CSV) -> None:
    """Upsert categories.csv into the table. Fails fast on a malformed file."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"{csv_path} has no categories")
    for r in rows:
        prio = r["default_priority"].strip().upper()
        if prio not in VALID_PRIORITIES:
            raise ValueError(f"bad priority {prio!r} for {r['category']}")
        conn.execute(
            """INSERT INTO categories(category, description, default_priority, sla_hours)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(category) DO UPDATE SET
                 description=excluded.description,
                 default_priority=excluded.default_priority,
                 sla_hours=excluded.sla_hours""",
            (r["category"].strip(), r["description"].strip(), prio, int(r["sla_hours"])),
        )
    conn.commit()


def init_db(path=DB_PATH, csv_path=CATEGORIES_CSV) -> sqlite3.Connection:
    conn = get_conn(path)
    conn.executescript(SCHEMA)
    load_categories(conn, csv_path)
    return conn


def get_categories(conn: sqlite3.Connection) -> dict:
    """{category: {description, default_priority, sla_hours}} - the closed list."""
    rows = conn.execute("SELECT * FROM categories").fetchall()
    return {
        r["category"]: {
            "description": r["description"],
            "default_priority": r["default_priority"],
            "sla_hours": r["sla_hours"],
        }
        for r in rows
    }
