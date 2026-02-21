import json
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import local
from typing import Dict, List, Optional, TypedDict

DB_PATH = Path("data/claw.db")
_local = local()

# ==============================================================================
# Database Types
# ==============================================================================

class OrderRow(TypedDict):
    order_id: str
    customer_phone: str
    status: str
    last_updated: str
    outreached: int

class SessionRow(TypedDict, total=False):
    session_id: str
    status: str
    channel: str
    message_history: str
    ai_enabled: int
    created_at: str
    last_message: str

class PendingActionArguments(TypedDict):
    order_id: str
    amount: float
    reason: str

class PendingActionRow(TypedDict):
    action_id: str
    session_id: str
    tool_name: str
    arguments: str  # JSON encoded string of PendingActionArguments
    reasoning: str
    tool_call_id: str
    created_at: str

class PendingActionUI(TypedDict):
    tool_name: str
    arguments: dict
    reasoning: str

class PausedSessionUI(TypedDict):
    session_id: str
    channel: str
    pending_action: PendingActionUI

class SettingRow(TypedDict):
    key: str
    value: str


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
            ai_enabled      INTEGER NOT NULL DEFAULT 1,
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pending_actions (
            action_id    TEXT PRIMARY KEY,
            session_id   TEXT NOT NULL REFERENCES sessions(session_id),
            tool_name    TEXT NOT NULL,
            arguments    TEXT NOT NULL,
            reasoning    TEXT,
            tool_call_id TEXT,
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    conn.commit()
    # Migration: add ai_enabled column for existing databases
    try:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN ai_enabled INTEGER NOT NULL DEFAULT 1"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Expected: column already exists after migration
    seed_orders()


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    row = _conn().execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def get_or_create_session(session_id: str, channel: str = "web") -> SessionRow:
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO sessions "
        "(session_id, status, channel, message_history, ai_enabled, created_at) "
        "VALUES (?, 'RUNNING', ?, '[]', 1, ?)",
        (session_id, channel, now),
    )
    conn.commit()
    return get_session(session_id)


def get_session(session_id: str) -> Optional[SessionRow]:
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


def try_transition_session(session_id: str, from_status: str, to_status: str) -> bool:
    """Atomically transition status only if the current status matches from_status.
    Returns True if the row was updated (this caller won), False if it was already
    in a different state (another caller got there first)."""
    conn = _conn()
    cur = conn.execute(
        "UPDATE sessions SET status = ? WHERE session_id = ? AND status = ?",
        (to_status, session_id, from_status),
    )
    conn.commit()
    return cur.rowcount == 1


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


def append_raw_message(session_id: str, message: dict) -> None:
    """Append a full message dict to history (e.g. assistant tool_calls or tool results)."""
    conn = _conn()
    row = conn.execute(
        "SELECT message_history FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    history = json.loads(row["message_history"]) if row else []
    history.append(message)
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


def get_all_sessions() -> List[SessionRow]:
    """Return all sessions ordered newest-first, with last_message computed."""
    rows = _conn().execute(
        "SELECT * FROM sessions ORDER BY created_at DESC"
    ).fetchall()
    result = []
    for row in rows:
        s = dict(row)
        history = json.loads(s.get("message_history", "[]"))
        last_msg = ""
        for msg in reversed(history):
            role = msg.get("role")
            content = msg.get("content")
            if role in ("user", "assistant") and content and isinstance(content, str):
                last_msg = content[:80]
                break
        s["last_message"] = last_msg
        result.append(s)
    return result


def set_ai_enabled(session_id: str, enabled: bool) -> None:
    conn = _conn()
    conn.execute(
        "UPDATE sessions SET ai_enabled = ? WHERE session_id = ?",
        (1 if enabled else 0, session_id),
    )
    conn.commit()


def append_agent_message(session_id: str, content: str) -> None:
    """Append a manual CS-agent reply (role='assistant', is_manual=True)."""
    conn = _conn()
    row = conn.execute(
        "SELECT message_history FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    history = json.loads(row["message_history"]) if row else []
    history.append({"role": "assistant", "content": content, "is_manual": True})
    conn.execute(
        "UPDATE sessions SET message_history = ? WHERE session_id = ?",
        (json.dumps(history), session_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Pending-action helpers
# ---------------------------------------------------------------------------

def save_pending_action(
    session_id: str,
    tool_name: str,
    arguments: dict,
    reasoning: str,
    tool_call_id: Optional[str] = None,
) -> str:
    action_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn = _conn()
    conn.execute(
        "INSERT INTO pending_actions "
        "(action_id, session_id, tool_name, arguments, reasoning, tool_call_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (action_id, session_id, tool_name, json.dumps(arguments), reasoning, tool_call_id, now),
    )
    conn.commit()
    return action_id


def get_pending_action(session_id: str) -> Optional[PendingActionRow]:
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

def get_all_paused_sessions() -> List[PausedSessionUI]:
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

def get_order(order_id: str) -> Optional[OrderRow]:
    """Return a single order row by ID, or None if not found."""
    row = _conn().execute(
        "SELECT * FROM orders WHERE order_id = ?", (order_id,)
    ).fetchone()
    return dict(row) if row else None


def _build_sql_query_for_filters(filters: dict):
    """Build a safe parameterised SQLite query from a filter dict.

    Supported keys:
        status (str)                — orders.status value (default 'delayed')
        min_hours_since_update (int)— only orders whose last_updated is older
        phone_prefix (str)          — customer_phone must start with this prefix
    Always implicitly filters outreached = 0.
    """
    conditions = ["status = ?", "outreached = 0"]
    params: list = [filters.get("status", "delayed")]

    if "min_hours_since_update" in filters:
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(hours=filters["min_hours_since_update"])
        ).isoformat()
        conditions.append("last_updated < ?")
        params.append(cutoff)

    if "phone_prefix" in filters:
        conditions.append("customer_phone LIKE ?")
        params.append(f"{filters['phone_prefix']}%")

    where_clause = " AND ".join(conditions)
    return "SELECT * FROM orders WHERE {0}".format(where_clause), params


def query_orders_by_filters(filters: dict) -> List[OrderRow]:
    """Query orders using a structured filter dict (used by the dynamic poller)."""
    query, params = _build_sql_query_for_filters(filters)
    rows = _conn().execute(query, params).fetchall()
    return [dict(row) for row in rows]


def query_stale_delayed_orders(hours: int = 24) -> List[OrderRow]:
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
