import json
import sqlite3
import uuid
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import local
from typing import Dict, List, Literal, Optional, TypedDict

# Session status constants — use these instead of bare strings
RUNNING: Literal["RUNNING"] = "RUNNING"
PAUSED:  Literal["PAUSED"]  = "PAUSED"
DONE:    Literal["DONE"]    = "DONE"
SessionStatus = Literal["RUNNING", "PAUSED", "DONE"]

DB_PATH = Path("data/claw.db")
_local = local()

# ==============================================================================
# Database Types
# ==============================================================================

class OrderRow(TypedDict):
    order_id: str
    customer_name: str
    customer_phone: str
    product_name: str
    item_count: int
    total_amount: float
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

class OrderEventRow(TypedDict):
    event_id: str
    order_id: str
    event_type: str
    description: str
    actor: str
    session_id: str
    created_at: str

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
            customer_name  TEXT NOT NULL DEFAULT '',
            customer_phone TEXT NOT NULL,
            product_name   TEXT NOT NULL DEFAULT '',
            item_count     INTEGER NOT NULL DEFAULT 1,
            total_amount   REAL NOT NULL DEFAULT 0.0,
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

        CREATE TABLE IF NOT EXISTS order_events (
            event_id    TEXT PRIMARY KEY,
            order_id    TEXT NOT NULL,
            event_type  TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            actor       TEXT NOT NULL DEFAULT 'system',
            session_id  TEXT,
            created_at  TEXT NOT NULL
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
    # Migration: add new order columns for existing databases
    for col_def in [
        ("customer_name", "TEXT NOT NULL DEFAULT ''"),
        ("product_name", "TEXT NOT NULL DEFAULT ''"),
        ("item_count", "INTEGER NOT NULL DEFAULT 1"),
        ("total_amount", "REAL NOT NULL DEFAULT 0.0"),
    ]:
        try:
            conn.execute(
                "ALTER TABLE orders ADD COLUMN {} {}".format(col_def[0], col_def[1])
            )
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Expected: column already exists after migration
    try:
        conn.execute("ALTER TABLE order_events ADD COLUMN session_id TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass
        
    seed_orders()
    seed_sessions()


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
    now = datetime.now(timezone.utc).isoformat()
    history.append({"role": role, "content": content, "timestamp": now})
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
    if "timestamp" not in message:
        message["timestamp"] = datetime.now(timezone.utc).isoformat()
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


def get_session_orders(session_id: str) -> set:
    """Return a set of order IDs associated with the session."""
    orders = set()
    for match in re.finditer(r'ORD-\d+', session_id):
        orders.add(match.group())

    for msg in get_history(session_id):
        if "tool_calls" in msg:
            for tc in msg.get("tool_calls", []):
                try:
                    args = json.loads(tc["function"]["arguments"])
                    if "order_id" in args:
                        orders.add(args["order_id"])
                except Exception:
                    pass

    rows = _conn().execute(
        "SELECT order_id FROM order_events WHERE session_id = ?", (session_id,)
    ).fetchall()
    for row in rows:
        orders.add(row["order_id"])
    return orders


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
    now = datetime.now(timezone.utc).isoformat()
    history.append({"role": "assistant", "content": content, "is_manual": True, "timestamp": now})
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


def mark_order_not_outreached(order_id: str) -> None:
    """Reset the outreached flag so the order can be re-triggered (used for demos)."""
    conn = _conn()
    conn.execute("UPDATE orders SET outreached = 0 WHERE order_id = ?", (order_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Order event helpers
# ---------------------------------------------------------------------------

def log_order_event(
    order_id: str,
    event_type: str,
    description: str = "",
    actor: str = "system",
    session_id: Optional[str] = None,
) -> str:
    """Log an interaction or state change to the order's timeline."""
    event_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn = _conn()
    conn.execute(
        "INSERT INTO order_events "
        "(event_id, order_id, event_type, description, actor, session_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event_id, order_id, event_type, description, actor, session_id, now),
    )
    conn.commit()
    return event_id


def get_order_timeline(order_id: str) -> List[OrderEventRow]:
    """Return all logged events for an order, formatted chronologically."""
    rows = _conn().execute(
        "SELECT * FROM order_events WHERE order_id = ? ORDER BY created_at ASC",
        (order_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_all_orders_with_event_count() -> List[Dict]:
    """Return all orders natively ordered, joined with a total event count."""
    rows = _conn().execute(
        """
        SELECT o.*, COALESCE(e.cnt, 0) AS event_count
        FROM orders o
        LEFT JOIN (
            SELECT order_id, COUNT(*) AS cnt FROM order_events GROUP BY order_id
        ) e ON e.order_id = o.order_id
        ORDER BY o.last_updated DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

def get_all_orders() -> List[OrderRow]:
    """Return all orders, newest first."""
    rows = _conn().execute(
        "SELECT * FROM orders ORDER BY last_updated DESC"
    ).fetchall()
    return [dict(row) for row in rows]


def seed_orders() -> None:
    now = datetime.now(timezone.utc)
    # (order_id, customer_name, customer_phone, product_name, item_count, total_amount, status, last_updated)
    orders = [
        ("ORD-001", "Alice Johnson",  "+1-555-0101", "Honolulu Sword & Shield J2NF Paddle",       1, 129.99, "processing", now.isoformat()),
        ("ORD-002", "Bob Martinez",   "+1-555-0102", "Selkirk VANGUARD Power Air Invikta",        1, 249.95, "delayed",    (now - timedelta(hours=48)).isoformat()),
        ("ORD-003", "Carol Chen",     "+1-555-0103", "Franklin X-40 Outdoor Pickleballs (12-pack)", 3,  35.97, "delivered",  now.isoformat()),
        ("ORD-004", "David Kim",      "+1-555-0104", "JOOLA Ben Johns Hyperion CFS 16mm",         1, 199.99, "delayed",    (now - timedelta(hours=72)).isoformat()),
        ("ORD-005", "Eva Rossi",      "+1-555-0105", "Pickleball Court Shoes - Skechers Viper",   2, 159.00, "processing", now.isoformat()),
        ("ORD-006", "Frank Okafor",   "+1-555-0106", "Onix Graphite Z5 Paddle",                   1,  89.00, "shipped",    (now - timedelta(hours=12)).isoformat()),
        ("ORD-007", "Grace Tanaka",   "+1-555-0107", "Pro Pickleball Bag & Accessories Kit",       4, 175.80, "delayed",    (now - timedelta(hours=36)).isoformat()),
        ("ORD-008", "Henry Dubois",   "+1-555-0108", "HEAD Extreme Tour Max Paddle",               1, 179.99, "cancelled",  (now - timedelta(hours=6)).isoformat()),
    ]
    conn = _conn()
    conn.executemany(
        "INSERT OR IGNORE INTO orders "
        "(order_id, customer_name, customer_phone, product_name, item_count, total_amount, status, last_updated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        orders,
    )
    conn.commit()
    # Seed order events for each order (only if no events exist yet)
    existing = conn.execute("SELECT COUNT(*) FROM order_events").fetchone()[0]
    if existing == 0:
        for o in orders:
            oid, cname, _, pname, count, total, status, ts = o
            placed_time = (now - timedelta(hours=96)).isoformat()
            conn.execute(
                "INSERT INTO order_events "
                "(event_id, order_id, event_type, description, actor, session_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), oid, "order_placed",
                 "{} x{} (${:.2f}) placed by {}".format(pname, count, total, cname),
                 "system", None, placed_time),
            )
            if status != "processing":
                conn.execute(
                    "INSERT INTO order_events "
                    "(event_id, order_id, event_type, description, actor, session_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), oid, "status_changed",
                     "Status changed to '{}'".format(status),
                     "system", None, ts),
                )
        conn.commit()


def seed_sessions() -> None:
    """Seed historical sessions into the database for demonstration purposes."""
    conn = _conn()
    existing = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    if existing > 0:
        return
        
    now = datetime.now(timezone.utc)
    
    # Create David Kim's demo session (ORD-004)
    sid = "telegram:183779"
    conn.execute(
        "INSERT OR IGNORE INTO sessions "
        "(session_id, status, channel, message_history, ai_enabled, created_at) "
        "VALUES (?, 'RUNNING', 'telegram', '[]', 1, ?)",
        (sid, (now - timedelta(hours=2)).isoformat()),
    )
    conn.commit()
    
    # We need to manually construct the history with custom timestamps
    history = [
        {
            "role": "user",
            "content": "Found 1 order(s): - ORD-004: David Kim - 'JOOLA Ben Johns Hyperion CFS 16mm' x1, $199.99, status=delayed",
            "timestamp": (now - timedelta(minutes=115)).isoformat()
        },
        {
            "role": "assistant",
            "content": "Thanks, David! I see you have one order (ORD-004) for a Joola Scorpeus 16mm racket, currently **delayed**. Let me know how I can help—whether you'd like an update on the expected delivery, to discuss the delay, or need any other assistance.",
            "timestamp": (now - timedelta(minutes=114)).isoformat()
        },
        {
            "role": "user",
            "content": "Refund please",
            "timestamp": (now - timedelta(minutes=80)).isoformat()
        },
        {
            "role": "user",
            "content": "Order ORD-004: customer='David Kim', product='JOOLA Ben Johns Hyperion CFS 16mm' x1, total=$199.99, status='delayed', last updated 2026-02-16T18:06:17.",
            "timestamp": (now - timedelta(minutes=79)).isoformat()
        },
        {
            "role": "assistant",
            "content": "We acknowledge your refund request for order ORD-004 and your reason for it (\"Customer requested refund due to delayed order\"). We will escalate this to an agent to help approve.",
            "timestamp": (now - timedelta(minutes=78)).isoformat()
        },
        {
            "role": "user",
            "content": "Refund of $199.99 issued for order ORD-004 - reason: Customer requested refund due to delayed order.",
            "timestamp": (now - timedelta(minutes=10)).isoformat()
        },
        {
            "role": "assistant",
            "content": "Your refund of **$199.99** for order **ORD-004** (JOOLA Ben Johns Hyperion CFS 16mm) has been processed and will be credited to your original payment method shortly. If you have any other questions, feel free to let me know!",
            "timestamp": (now - timedelta(minutes=9)).isoformat()
        }
    ]
    
    conn.execute(
        "UPDATE sessions SET message_history = ? WHERE session_id = ?",
        (json.dumps(history), sid)
    )
    conn.commit()
    
    # Inject an event to tie it explicitly in the db if needed
    conn.execute(
        "INSERT INTO order_events "
        "(event_id, order_id, event_type, description, actor, session_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), "ORD-004", "refund_issued",
         "Refund of $199.99 issued - reason: Customer requested refund due to delayed order.",
         "operator", sid, (now - timedelta(minutes=10)).isoformat()),
    )
    conn.commit()
