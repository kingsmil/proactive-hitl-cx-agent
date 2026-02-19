import json
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import local
from typing import Dict, List, Optional

DB_PATH = Path("data/claw.db")
_local = local()


def _conn() -> sqlite3.Connection:
    """Return a thread-local SQLite connection, creating it if needed."""
    if not hasattr(_local, "conn"):
        DB_PATH.parent.mkdir(exist_ok=True)
        _local.conn = sqlite3.connect(str(DB_PATH))
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


def init_db() -> None:
    conn = _conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id       TEXT PRIMARY KEY,
            customer_phone TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'processing',
            last_updated   TEXT NOT NULL,
            outreached     INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS sessions (
            session_id      TEXT PRIMARY KEY,
            status          TEXT NOT NULL DEFAULT 'RUNNING',
            channel         TEXT NOT NULL DEFAULT 'web',
            message_history TEXT NOT NULL DEFAULT '[]',
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pending_actions (
            action_id   TEXT PRIMARY KEY,
            session_id  TEXT NOT NULL REFERENCES sessions(session_id),
            tool_name   TEXT NOT NULL,
            arguments   TEXT NOT NULL,
            reasoning   TEXT,
            created_at  TEXT NOT NULL
        );
    """)
    conn.commit()
    seed_orders()


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def get_or_create_session(session_id: str, channel: str = "web") -> Dict:
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO sessions (session_id, status, channel, message_history, created_at) "
        "VALUES (?, 'RUNNING', ?, '[]', ?)",
        (session_id, channel, now),
    )
    conn.commit()
    return get_session(session_id)


def get_session(session_id: str) -> Optional[Dict]:
    row = _conn().execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    return dict(row) if row else None


def set_session_status(session_id: str, status: str) -> None:
    conn = _conn()
    conn.execute(
        "UPDATE sessions SET status = ? WHERE session_id = ?", (status, session_id)
    )
    conn.commit()


def append_message(session_id: str, role: str, content: str) -> None:
    conn = _conn()
    row = conn.execute(
        "SELECT message_history FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    history = json.loads(row["message_history"]) if row else []
    history.append({"role": role, "content": content})
    conn.execute(
        "UPDATE sessions SET message_history = ? WHERE session_id = ?",
        (json.dumps(history), session_id),
    )
    conn.commit()


def get_history(session_id: str) -> List[Dict]:
    row = _conn().execute(
        "SELECT message_history FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    return json.loads(row["message_history"]) if row else []


# ---------------------------------------------------------------------------
# Pending-action helpers
# ---------------------------------------------------------------------------

def save_pending_action(
    session_id: str, tool_name: str, arguments: dict, reasoning: str
) -> str:
    action_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn = _conn()
    conn.execute(
        "INSERT INTO pending_actions (action_id, session_id, tool_name, arguments, reasoning, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (action_id, session_id, tool_name, json.dumps(arguments), reasoning, now),
    )
    conn.commit()
    return action_id


def get_pending_action(session_id: str) -> Optional[Dict]:
    row = _conn().execute(
        "SELECT * FROM pending_actions WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["arguments"] = json.loads(result["arguments"])
    return result


def delete_pending_action(session_id: str) -> None:
    conn = _conn()
    conn.execute("DELETE FROM pending_actions WHERE session_id = ?", (session_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# HITL queue — enriched list for the UI
# ---------------------------------------------------------------------------

def get_all_paused_sessions() -> List[Dict]:
    rows = _conn().execute(
        """
        SELECT s.session_id, s.channel,
               pa.tool_name, pa.arguments, pa.reasoning
        FROM sessions s
        JOIN pending_actions pa ON pa.session_id = s.session_id
        WHERE s.status = 'PAUSED'
        ORDER BY pa.created_at ASC
        """
    ).fetchall()

    result = []
    for row in rows:
        result.append(
            {
                "session_id": row["session_id"],
                "channel": row["channel"],
                "pending_action": {
                    "tool_name": row["tool_name"],
                    "arguments": json.loads(row["arguments"]),
                    "reasoning": row["reasoning"],
                },
            }
        )
    return result


# ---------------------------------------------------------------------------
# CRM / poller helpers
# ---------------------------------------------------------------------------

def query_stale_delayed_orders(hours: int = 24) -> List[Dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = _conn().execute(
        "SELECT * FROM orders WHERE status = 'delayed' AND last_updated < ? AND outreached = 0",
        (cutoff,),
    ).fetchall()
    return [dict(row) for row in rows]


def mark_order_outreached(order_id: str) -> None:
    conn = _conn()
    conn.execute("UPDATE orders SET outreached = 1 WHERE order_id = ?", (order_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

def seed_orders() -> None:
    now = datetime.now(timezone.utc)
    orders = [
        ("ORD-001", "+1-555-0101", "processing", now.isoformat()),
        ("ORD-002", "+1-555-0102", "delayed",    (now - timedelta(hours=48)).isoformat()),
        ("ORD-003", "+1-555-0103", "delivered",  now.isoformat()),
        ("ORD-004", "+1-555-0104", "delayed",    (now - timedelta(hours=72)).isoformat()),
        ("ORD-005", "+1-555-0105", "processing", now.isoformat()),
    ]
    conn = _conn()
    conn.executemany(
        "INSERT OR IGNORE INTO orders (order_id, customer_phone, status, last_updated) "
        "VALUES (?, ?, ?, ?)",
        orders,
    )
    conn.commit()
