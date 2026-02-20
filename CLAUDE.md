# CustomerClaw — Implementation Guide

## Project Overview

**CustomerClaw** is a proactive, Human-in-the-Loop (HITL) customer-experience agent.
A FastAPI backend drives a framework-free LLM orchestrator that handles inbound
customer messages, monitors a mock CRM for stale orders, and gates irreversible
actions (e.g. issuing refunds) behind operator approval before executing them.

The operator-facing UI ("Operator's Sanctum") is a three-pane dashboard already
fully built in Jinja2 + HTMX. The entire backend is yet to be written.

---

## What Is Already Built

```
frontend/
  templates/
    base.html                      # Full CSS design system (Frieren dark theme)
    dashboard.html                 # Three-pane operator dashboard
    partials/
      chat_exchange.html           # User + agent message pair bubble
      thought_entry.html           # Single trace entry (node tag + text)
      action_card.html             # HITL card with Grant / Deny buttons
      action_queue.html            # Full pending-seals pane (loops action_card)
      action_decision.html         # Post-decision result (approved / rejected)
      error_toast.html             # Agent error overlay (rose palette, auto-dismiss)
```

### Template Variable Contract (do NOT break these)

| Template | Required variables |
|---|---|
| `dashboard.html` | `session_id` |
| `chat_exchange.html` | `user_message`, `channel`, `agent_message`, `pending` (bool) |
| `thought_entry.html` | `node` (`supervisor`\|`reason`\|`execute`\|`hitl`\|`unknown`), `preview` |
| `action_card.html` | `session` (obj with `.session_id`, `.channel`, `.pending_action`) |
| `action_queue.html` | `pending_sessions` (list of session objects) |
| `action_decision.html` | `decision` (`approved`\|`rejected`), `session_id` |
| `error_toast.html` | `message` (plain-text error string) |

### Settings Component

`partials/settings_modal.html` — modal form loaded on demand into `#settings-modal` via
gear button in the header (`hx-get="/settings" hx-target="#settings-modal"`).

Variables: `model` (current model ID string), `has_key` (bool — key is set in DB).

`GET /settings` returns the modal. `POST /settings` accepts form fields `model` and
`openrouter_api_key` and writes non-empty values to the `settings` DB table.

The `settings` table (`key TEXT PRIMARY KEY, value TEXT`) stores runtime overrides.
`db.get_setting(key, default)` / `db.set_setting(key, value)` are the helpers.
`agent.call_llm()` reads `"model"` and `"openrouter_api_key"` from DB on every call
(falling back to `DEFAULT_MODEL` and `$OPENROUTER_API_KEY` env var respectively).

### Error Toast Component

`partials/error_toast.html` is rendered by `agent.emit_error(session_id, message)` and
pushed into the SSE queue as a named event dict: `{"event": "error", "data": html}`.

`#error-toast` in `dashboard.html` lives inside `#thought-log` (the SSE-connected element)
and carries `sse-swap="error" hx-swap="innerHTML"`. When a named `error` SSE frame
arrives, HTMX replaces its inner HTML, triggering the CSS toast-in / toast-out animations.

The div is `position: fixed; top: 20px; right: 20px` so it overlays the viewport
regardless of its DOM parent. Styled with `.toast-error` (rose palette, Cinzel title,
Inconsolata body). Auto-dismisses via `@keyframes toast-out` at 5 s.

The SSE generator in `api/app.py` must yield dict items directly from the queue
(not wrapped again); string items continue to be yielded as `{"data": html}`.

### API Endpoints wired into the frontend (HTMX)

| Method | Path | Called by |
|---|---|---|
| `POST` | `/chat/message` | Chat input form |
| `GET` | `/agent/thoughts/{session_id}` | SSE stream → Scrying Glass pane |
| `GET` | `/actions/pending` | Polled every 5 s → Pending Seals pane |
| `POST` | `/actions/approve/{session_id}` | Grant button on action card |
| `POST` | `/actions/reject/{session_id}` | Deny button on action card |

---

## What Needs to Be Built

Everything backend. Follow the phases below in order.

---

## Phase 1 — State Layer (SQLite)

**File:** `db.py`

### Tables

```sql
-- Mock CRM
CREATE TABLE IF NOT EXISTS orders (
    order_id       TEXT PRIMARY KEY,
    customer_phone TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'processing',
    last_updated   TEXT NOT NULL   -- ISO-8601 datetime
);

-- Agent sessions
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'RUNNING',  -- RUNNING | PAUSED | DONE
    channel         TEXT NOT NULL DEFAULT 'web',
    message_history TEXT NOT NULL DEFAULT '[]',       -- JSON array
    created_at      TEXT NOT NULL
);

-- HITL gate
CREATE TABLE IF NOT EXISTS pending_actions (
    action_id   TEXT PRIMARY KEY,  -- uuid4
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    tool_name   TEXT NOT NULL,
    arguments   TEXT NOT NULL,     -- JSON object
    reasoning   TEXT,
    created_at  TEXT NOT NULL
);
```

### Helper functions to implement

```python
def get_or_create_session(session_id: str, channel: str = "web") -> dict
def get_session(session_id: str) -> dict | None
def set_session_status(session_id: str, status: str) -> None
def append_message(session_id: str, role: str, content: str) -> None
def get_history(session_id: str) -> list[dict]
def save_pending_action(session_id: str, tool_name: str, arguments: dict, reasoning: str) -> str
def get_pending_action(session_id: str) -> dict | None
def delete_pending_action(session_id: str) -> None
def get_all_paused_sessions() -> list[dict]
def seed_orders() -> None  # Insert a handful of mock orders, some with status='delayed'
```

---

## Phase 2 — FastAPI Application Skeleton

**File:** `main.py`

```python
app = FastAPI()
app.mount("/static", StaticFiles(...))
templates = Jinja2Templates(directory="frontend/templates")
```

### Endpoints to implement

**`GET /`** — Redirect to `/chat/{new_uuid}`

**`GET /chat/{session_id}`** — Render `dashboard.html` with `session_id`

**`POST /chat/message`** — Accept form fields `session_id`, `message`.
  1. Call `db.append_message(session_id, "user", message)`.
  2. `db.set_session_status(session_id, "RUNNING")`.
  3. Enqueue `background_tasks.add_task(run_agent, session_id)`.
  4. Return `TemplateResponse("partials/chat_exchange.html", {..., pending=True})`.

**`GET /agent/thoughts/{session_id}`** — SSE endpoint.
  Stream `EventSourceResponse` backed by a per-session `asyncio.Queue` stored
  in a module-level dict `thought_queues: dict[str, asyncio.Queue]`.

**`GET /actions/pending`** — Query all PAUSED sessions.
  Return `TemplateResponse("partials/action_queue.html", {pending_sessions: [...]})`.

**`POST /actions/approve/{session_id}`**
  1. `db.set_session_status(session_id, "RUNNING")`.
  2. Enqueue `background_tasks.add_task(run_agent, session_id)`.
  3. Return `TemplateResponse("partials/action_decision.html", {decision:"approved", ...})`.

**`POST /actions/reject/{session_id}`**
  1. `db.delete_pending_action(session_id)`.
  2. `db.append_message(session_id, "tool", "Action rejected by operator.")`.
  3. `db.set_session_status(session_id, "RUNNING")`.
  4. Enqueue `background_tasks.add_task(run_agent, session_id)`.
  5. Return `TemplateResponse("partials/action_decision.html", {decision:"rejected", ...})`.

---

## Phase 3 — Agent Worker (Core Loop)

**File:** `agent.py`

### Thought streaming helper

```python
async def emit_thought(session_id: str, node: str, preview: str) -> None:
    """Push a rendered thought_entry partial into the SSE queue."""
```

### Tool registry

Define two dictionaries:
- `SAFE_TOOLS` — tools executed immediately without approval.
- `HITL_TOOLS` — tools that pause the session and await human approval.

### Mock tools to implement

```python
# SAFE
def check_order_status(order_id: str) -> str:
    """Query the orders table and return a human-readable status string."""

# HITL
def issue_refund(order_id: str, amount: float, reason: str) -> str:
    """Stub — never actually called; existence triggers the HITL gate."""
```

### Tool schema (passed to the LLM)

Describe both tools using the **OpenAI function-calling format** — this is what
OpenRouter expects regardless of which underlying model is selected:

```python
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_order_status",
            "description": "Look up the current status of a customer order.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "The order identifier, e.g. ORD-001"}
                },
                "required": ["order_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "issue_refund",
            "description": "Issue a full or partial refund to a customer. Requires operator approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "amount":   {"type": "number", "description": "Refund amount in USD"},
                    "reason":   {"type": "string"}
                },
                "required": ["order_id", "amount", "reason"]
            }
        }
    }
]
```

### Orchestrator loop

```python
async def run_agent(session_id: str) -> None:
    while db.get_session(session_id)["status"] == "RUNNING":
        history = db.get_history(session_id)
        await emit_thought(session_id, "supervisor", "Evaluating conversation…")

        response = call_llm(history, tools=TOOLS)  # OpenRouter

        choice = response.choices[0]
        msg    = choice.message

        if choice.finish_reason == "tool_calls":
            for tool_call in msg.tool_calls:
                name = tool_call.function.name
                args = json.loads(tool_call.function.arguments)

                if name in SAFE_TOOLS:
                    await emit_thought(session_id, "execute", f"→ {name}")
                    result = SAFE_TOOLS[name](**args)
                    db.append_message(session_id, "tool", result)
                    # loop continues

                elif name in HITL_TOOLS:
                    await emit_thought(session_id, "hitl", f"⚠ {name} requires approval")
                    db.save_pending_action(session_id, name, args, reasoning=msg.content)
                    db.set_session_status(session_id, "PAUSED")
                    return  # halt — resume via /actions/approve

        else:  # finish_reason == "stop" — final text response
            await emit_thought(session_id, "reason", "Composing reply…")
            db.append_message(session_id, "assistant", msg.content)
            db.set_session_status(session_id, "DONE")
            return
```

### LLM Integration

Use the **OpenAI Python SDK** pointed at OpenRouter's base URL.
OpenRouter proxies any model using the same OpenAI-compatible interface.
Default model: `anthropic/claude-3.5-haiku` for low latency in the loop;
swap to `anthropic/claude-3.5-sonnet` for higher-quality responses.

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPENROUTER_API_KEY"],
)

def call_llm(history: list[dict], tools: list[dict]):
    return client.chat.completions.create(
        model="anthropic/claude-3.5-haiku",
        messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history,
        tools=tools,
        tool_choice="auto",
    )
```

System prompt should establish the agent as a customer-support assistant that
has access to order-lookup and refund tools, and must use them appropriately.

---

## Phase 4 — Proactive CRM Poller

**File:** `poller.py`

Use **APScheduler** (`BackgroundScheduler`) to run every 60 seconds.

### Logic

```python
def poll_crm() -> None:
    stale_orders = db.query_stale_delayed_orders(hours=24)
    for order in stale_orders:
        sid = f"proactive-{order['order_id']}"
        db.get_or_create_session(sid, channel="proactive")
        synthetic_msg = (
            f"[System] Order {order['order_id']} for customer "
            f"{order['customer_phone']} has been delayed for over 24 hours. "
            "Draft a proactive outreach message."
        )
        db.append_message(sid, "user", synthetic_msg)
        db.set_session_status(sid, "RUNNING")
        asyncio.run(run_agent(sid))  # or schedule via background queue
```

Add `db.mark_order_outreached(order_id)` to prevent duplicate triggers.

Mount the scheduler startup/shutdown via FastAPI `lifespan`.

---

## Phase 5 — Wiring & Polish

### `requirements.txt`

```
fastapi
uvicorn[standard]
openai
apscheduler
sse-starlette
jinja2
python-multipart
```

### Environment

```
OPENROUTER_API_KEY=sk-or-...
```

### Running

```bash
uvicorn main:app --reload --port 8000
```

### HITL flow (end-to-end test path)

1. Open `http://localhost:8000/` — auto-redirected to a fresh session.
2. Type: *"I'd like a refund for order ORD-001"*.
3. Scrying Glass shows thought trace; chat bubble shows "awaiting the seal".
4. Pending Seals pane populates with the `issue_refund` action card.
5. Click **Grant** — worker resumes, LLM generates confirmation, chat updates.

---

## Architecture Diagram

```
Clients (Web UI / Proactive Poller)
         │
         ▼
   FastAPI (main.py)
   ├── POST /chat/message     ──► append msg → enqueue run_agent
   ├── GET  /agent/thoughts   ──► SSE stream (asyncio.Queue per session)
   ├── GET  /actions/pending  ──► query PAUSED sessions
   ├── POST /actions/approve  ──► flip RUNNING → enqueue run_agent
   └── POST /actions/reject   ──► inject rejection → enqueue run_agent
         │
         ▼
   Agent Worker (agent.py)
   ├── emit_thought() ──► SSE queue
   ├── call_llm()     ──► OpenAI SDK → OpenRouter (claude-3.5-haiku / sonnet)
   ├── SAFE_TOOLS     ──► execute immediately, loop
   └── HITL_TOOLS     ──► save pending_action, PAUSED, return
         │
         ▼
   SQLite (db.py)
   ├── sessions
   ├── pending_actions
   └── orders (mock CRM)
         ▲
         │
   APScheduler (poller.py)
   └── poll_crm() every 60 s ──► inject synthetic messages for stale orders
```

---

## Key Design Decisions

- **No agent framework** — the orchestrator is a plain `while` loop.
- **SQLite for all state** — sessions, HITL queue, and mock CRM in one file.
- **SSE not WebSockets** — `sse-starlette` is simpler; HTMX ext handles reconnect.
- **HTMX over JS** — all UI updates are server-rendered HTML fragments.
- **OpenRouter via OpenAI SDK** — `base_url="https://openrouter.ai/api/v1"` with
  `OPENROUTER_API_KEY`. Default model `anthropic/claude-3.5-haiku`; swap to
  `anthropic/claude-3.5-sonnet` for higher quality. Tool-use uses the standard
  OpenAI function-calling format (`finish_reason == "tool_calls"`).
