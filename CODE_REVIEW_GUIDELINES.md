# CustomerClaw — Code Review Guidelines

> **Purpose:** This document is the authoritative review checklist for every pull request / code change
> to CustomerClaw. Reviewers must apply it rigorously. It also serves as the north star for all new
> development. Priority order: **Orthogonality → Simplicity → Readability → EDA compliance**.

---

## Table of Contents

1. [Architecture Principles](#1-architecture-principles)
2. [Orthogonality](#2-orthogonality)
3. [Simplicity](#3-simplicity)
4. [Readability](#4-readability)
5. [EDA (Event-Driven Architecture) Compliance](#5-eda-event-driven-architecture-compliance)
6. [Python-Specific Standards](#6-python-specific-standards)
7. [Testing Standards](#7-testing-standards)
8. [Live Issues & Outstanding Debt](#8-live-issues--outstanding-debt)
9. [Review Checklist (Quick Reference)](#9-review-checklist-quick-reference)

---

## 1. Architecture Principles

CustomerClaw is built on four explicit design choices documented in `CLAUDE.md`:

| Principle | Implication for reviewers |
|---|---|
| No agent framework — plain `while` loop | Do **not** introduce LangChain, LlamaIndex, etc. |
| SQLite for all state | Do **not** introduce Redis, in-memory dicts for persistent state, or a second DB. |
| SSE not WebSockets | All server-push uses `sse_starlette`; do **not** add WebSockets unless the architecture is reconsidered. |
| HTMX over JS | All UI updates are server-rendered HTML fragments. Do **not** add React/Vue components; keep JS to HTMX directives. |

**Any PR that violates a design decision must explicitly justify the deviation in the PR description
and get explicit team sign-off before merge.**

---

## 2. Orthogonality

> *Each module should do one thing, and only one thing. A change in module A must never silently
> require a matching change in module B.*

### 2.1 Layer Boundaries

The codebase has four layers. Reviewers must enforce the boundary between them:

```
api/routes/      ← HTTP concerns only (request parsing, response rendering)
agent/           ← Orchestration, LLM calls, SSE emission
db/              ← State persistence (SQLite)
frontend/        ← Presentation (Jinja2 templates, HTMX)
```

**Violations to reject:**

- Any `db.*` call inside `frontend/templates/` (Jinja2 should receive pre-fetched data).
- Any HTML string construction inside Python files (breaks the `agent` ↔ `frontend` boundary).
- Any direct `sqlite3` calls outside `db/__init__.py` (the tool function `check_order_status`
  currently violates this — see [§8](#8-live-issues--outstanding-debt)).
- Any route logic (branching on request params) inside `agent/`.

### 2.2 The `tools.py` / `db` Violation (Known Debt)

`agent/tools.py:9` calls `db._conn()` directly:

```python
# ❌ CURRENT — tool talks to private db internals
row = db._conn().execute(
    "SELECT status, last_updated FROM orders WHERE order_id = ?", (order_id,)
).fetchone()
```

**Correct pattern:** expose a proper `db` helper and call it:

```python
# ✅ TARGET
def get_order(order_id: str) -> Optional[OrderRow]:  # in db/__init__.py
    ...

# agent/tools.py
def check_order_status(order_id: str) -> str:
    row = db.get_order(order_id)
    ...
```

No new tool function may ever call `db._conn()` directly. Private symbols (underscore-prefixed)
are an implementation detail of their module.

### 2.3 SSE Queue Duplication

`agent/__init__.py` and `agent/sse_events.py` **both** define `_ensure_stream_queue()`:

```python
# agent/__init__.py (line 28-30)  — duplicate
def _ensure_stream_queue(session_id):
    if session_id not in stream_queues:
        stream_queues[session_id] = asyncio.Queue()

# agent/sse_events.py (line 14-16) — canonical location
def _ensure_stream_queue(session_id):
    if session_id not in stream_queues:
        stream_queues[session_id] = asyncio.Queue()
```

**Guideline:** One definition per behaviour. `agent/__init__.py` must import and call the
version from `sse_events`; the duplicate must be removed.

### 2.4 Queue Initialization Spread

`thought_queues[session_id] = asyncio.Queue()` is created in at least four places:
`run_agent`, `emit_thought`, `emit_llm_thought`, `emit_error`, and `sse.py:thought_stream`.
**All queue initialisation must flow through a single factory/guard** — either a dedicated
helper or the `sse_events._ensure_thought_queue()` function (to be added).

---

## 3. Simplicity

> *The simplest code that correctly solves the problem is always preferred. Every line of complexity
> must earn its existence.*

### 3.1 Magic String Literals

**Status literals** (`"RUNNING"`, `"PAUSED"`, `"DONE"`) appear as bare strings in at least
eight different files. A typo creates a silent bug:

```python
# ❌ Current — magic string in actions.py
if not db.try_transition_session(session_id, "PAUSED", "RUNNING"):
```

**Guideline:** Define a single `SessionStatus` enum (or `Literal` type alias) in `db/__init__.py`
and use it everywhere:

```python
from db import SessionStatus   # "RUNNING" | "PAUSED" | "DONE"
```

### 3.2 Inline Acknowledgement Messages in the Orchestrator

`agent/__init__.py:146-159` contains a hardcoded tool-specific acknowledgement string for
`issue_refund`:

```python
# ❌ Business copy inside the orchestration loop
if name == "issue_refund":
    ack = ("We acknowledge your refund request for order {order_id}...")
else:
    ack = ("We acknowledge your request...")
```

**Guideline:** Tool-specific acknowledgement copy belongs in `tools.py` next to the tool it
describes, or in a dedicated `TOOL_ACK_MESSAGES: dict[str, str]` constant. The orchestrator
must not branch on tool names for copy purposes.

### 3.3 JSON Truncation Workaround

A fragile string-manipulation pattern appears in two places:

```python
# agent/__init__.py:122-123 and agent/sse_events.py:64-65
if args_raw.rfind("}") != -1:
    args_raw = args_raw[:args_raw.rfind("}") + 1]
```

This is a workaround for LLM streaming artifacts. It must:
1. Live in **one** place (a private helper, e.g. `_sanitize_json_fragment(s: str) -> str`).
2. Have a unit test that proves the edge cases it covers.
3. Have a comment citing **why** this is needed (i.e., which LLM provider generates the artifact).

### 3.4 Avoid Overloaded Functions

`call_llm_streaming` accepts an optional `push_chunk_callback` but has a streaming HTTP loop
regardless. If no callback is needed, `call_llm` (non-streaming) should be used. **Never**
pass `None` to `push_chunk_callback`; document clearly which call sites need streaming vs. not.

### 3.5 Config File is Trivial

`api/routes/config.py` is 3 lines:

```python
from fastapi.templating import Jinja2Templates
templates = Jinja2Templates(directory="frontend/templates")
```

This is not a config module—it is a singleton instantiation. Either:
- Move it into `api/app.py` and import from there, or
- Name it `api/templates.py` to reflect what it actually is.

---

## 4. Readability

> *Code is read ten times more than it is written. Write for the next engineer, not the compiler.*

### 4.1 Type Annotations

All public functions must have full type annotations. `Optional[X]` is preferred over `X | None`
for Python < 3.10 compatibility (the codebase does not pin a Python version).

**Status:** `db/__init__.py` is well-annotated (TypedDicts, return types). `agent/__init__.py`
is partially annotated. `api/routes/*.py` functions lack return type annotations.

```python
# ❌ Missing return type
def chat_page(request: Request, session_id: str):

# ✅ Correct
def chat_page(request: Request, session_id: str) -> TemplateResponse:
```

### 4.2 Docstrings

Every public function must have a one-line docstring describing **what it does** (not how).
Private helpers (`_` prefix) must have a docstring unless their name is entirely self-evident.
The following currently lack docstrings: `root()`, `chat_page()`, `get_chat_pane()`,
`customer_message()`, `toggle_ai()`, all DB status helpers.

### 4.3 Inline HTML in Route Responses

`api/routes/actions.py:57-61` assembles an HTML response string inline in a Python function:

```python
# ❌ Inline HTML — breaks IDE syntax, untestable, not themeable
return HTMLResponse(
    '<div class="action-card result-rejected" style="opacity:0.6;">'
    "⚠ Already handled by another operator — "
    '<code style="font-size:9px;">{}</code></div>'.format(session_id)
)
```

**Guideline:** Any UI string that a human will see must live in a Jinja2 template under
`frontend/templates/partials/`. The `_already_handled` helper must be refactored to use
`templates.TemplateResponse("partials/action_already_handled.html", {...})`.

Similarly, `api/routes/settings.py:33` returns a raw `<span>` string. This must also move to a
template.

### 4.4 Naming Conventions

| Context | Rule | Example |
|---|---|---|
| Route path params | `snake_case` | `session_id`, not `sessionId` |
| Template variables | `snake_case` | `pending_count`, not `pendingCount` |
| Python constants | `UPPER_SNAKE` | `DEFAULT_MODEL`, `OPENROUTER_URL` |
| Private module helpers | Leading `_` | `_build_llm_request_payload` |
| TypedDict keys | Match DB column names exactly | `session_id`, `tool_call_id` |

**Violation:** `api/routes/actions.py` uses `pending_count` correctly in the approve path,
but calls `get_all_paused_sessions()` inline in the reject path (different logic applied to the
same concept). Unify to a single helper call.

### 4.5 Line Length

Target **100 chars** per line. The current codebase has several over-long lines that force
horizontal scrolling:

```python
# ❌ Too long — actions.py:52
{\"request\": request, \"decision\": \"rejected\", \"session_id\": session_id, \"pending_count\": len(db.get_all_paused_sessions())},
```

**Guideline:** Extract template context dicts into named variables before passing them to
`TemplateResponse`.

### 4.6 Comment Quality

Comments must explain **why**, not **what**. The code already shows what it does.

```python
# ❌ Redundant — explains the what
# loop continues

# ✅ Explains the why
# Session status stays RUNNING so the while-loop continues naturally;
# no explicit state change needed.
```

Good examples already in the codebase: the CAS comment in `actions.py:22-23`, the `_ssl_ctx`
comment in `llm_client.py`. Use these as a template.

---

## 5. EDA (Event-Driven Architecture) Compliance

CustomerClaw uses SSE as its event bus between the backend agent and the browser. The following
rules ensure the event model stays coherent.

### 5.1 Named Event Contract

The current named SSE events are:

| Event name | Queue | Consumer | Purpose |
|---|---|---|---|
| *(unnamed)* | `thought_queues` | Scrying Glass pane | Thought trace entries |
| `error` | `thought_queues` | `#error-toast` | Error overlay |
| `chunk` | `stream_queues` | Chat bubble | Streaming token |
| `done` | `stream_queues` | Chat bubble | Final reply + OOB badge |
| `append` | `stream_queues` | Chat bubble | Non-streamed message appended |

**Guideline:** This contract must be documented and kept current. Any new named event requires:
1. An entry added to the table above in this document.
2. A corresponding `hx-on:sse:…` or `sse-swap` attribute in the relevant template.
3. A new `emit_*` function in `agent/sse_events.py` (not inline in `__init__.py`).

### 5.2 Queue Lifecycle

- Queues are created on-demand and **never deleted**. For long-running deployments this is a
  memory leak. A future PR must implement queue eviction when a session's status is `DONE` and
  its SSE client has disconnected.
- The `timeout=15.0` keepalive in `sse.py` is intentional — do not remove it.

### 5.3 Thread Safety

The LLM streaming call runs inside `asyncio.to_thread()` (thread pool), while the SSE queues
live on the asyncio event loop. The bridge is:

```python
loop.call_soon_threadsafe(stream_queues[session_id].put_nowait, token_html)
```

**Guideline:** Never call `await queue.put(...)` from a thread. Never call
`queue.put_nowait(...)` from the event loop when backpressure matters (the queue is unbounded;
use `put_nowait` only for small tokens). Do not add new cross-thread queue writes without
documenting the threading model.

### 5.4 OOB Swaps Are a UI Side-Effect, Not an Event

The `oob_badge` string injected into `emit_stream_done()` couples the agent logic to UI
badge rendering. This is pragmatic but fragile:

```python
# agent/__init__.py:163-166
oob_badge = (
    '<span id="queue-count" hx-swap-oob="innerHTML">'
    '{} awaiting</span>'
).format(pending_count)
```

**Guideline:** OOB HTML must come from a template partial, not from a format string in the
orchestrator. Extract to `partials/queue_badge.html`.

### 5.5 Poller Is Not Implemented

`poller/__init__.py` is a stub comment. When Phase 4 is implemented:

- The poller **must not** call `asyncio.run(run_agent(sid))` from a thread (deadlocks if called
  from inside a running event loop). Use `asyncio.get_event_loop().create_task()` or a
  background_task.
- Each proactive session ID must be deterministic and idempotent: `f"proactive-{order_id}"` is
  correct — do not use a fresh UUID.
- `db.mark_order_outreached()` must be called **before** `run_agent()` to prevent duplicate
  triggers on re-entry.

---

## 6. Python-Specific Standards

### 6.1 No Bare `except`

```python
# ❌ Swallows all exceptions silently — db/__init__.py:114
except Exception:
    pass  # Column already exists
```

```python
# ✅ Narrow the exception; log if this path is ever unexpected
except sqlite3.OperationalError:
    pass  # Expected: column already exists after migration
```

### 6.2 `Optional` Parameters with Mutable Defaults

```python
# ❌ Mutable default — db/__init__.py:275
def save_pending_action(..., tool_call_id: str = None) -> str:
```

`tool_call_id` should be typed `Optional[str] = None`. The annotation must reflect the type
system correctly.

### 6.3 Environment Variables

Environment variables must be read at call time (already done in `_build_llm_endpoint_config`),
**not** at module import time. This ensures test isolation. Never use `os.environ["KEY"]`
(raises `KeyError` on missing); always use `os.environ.get("KEY", "")` with a documented
default, or raise a descriptive error at startup.

### 6.4 Path Literals

```python
# ❌ Relative string path — breaks if cwd changes
_jinja = Environment(loader=FileSystemLoader("frontend/templates"))
```

```python
# ✅ Resolve relative to this file's directory
from pathlib import Path
_TEMPLATES_DIR = Path(__file__).parent.parent / "frontend" / "templates"
_jinja = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)))
```

`db/__init__.py` already uses `Path("data/claw.db")` correctly for the database path
(relative to the process cwd, which is the project root). `sse_events.py` must follow
the same pattern.

### 6.5 `pyproject.toml` — Pin a Python Version

The project has no `requires-python` constraint. Add one to prevent incompatible deploys:

```toml
[project]
requires-python = ">=3.11"
```

---

## 7. Testing Standards

### 7.1 What Exists

`tests/test_db.py` is excellent: 45 isolated unit tests, each with its own temporary SQLite
database, covering happy paths, edge cases, and concurrency semantics (CAS tests). This is
the gold standard for all new tests.

### 7.2 What Is Missing

| Gap | Priority |
|---|---|
| `agent/tools.py` — `check_order_status` and `issue_refund` have no tests | High |
| `agent/llm_client.py` — LLM request building (`_build_llm_request_payload`, `_accumulate_tool_call_argument_deltas`) has no tests | High |
| `api/routes/` — all endpoints have no integration tests | Medium |
| `agent/__init__.py` — orchestrator logic (`run_agent`) has no tests | Medium |
| Streaming SSE token pipeline | Low (browser agent acceptable substitute) |

### 7.3 Test Rules

1. **Unit tests must be hermetic.** No network calls, no shared global state, no file system
   mutations outside `tempfile.mkdtemp()`.
2. **Each test class must have a clear `setUp` / `tearDown`** that returns the system to a
   clean state. Copy `DBTestCase` in `test_db.py` as the base pattern.
3. **Test names must describe the scenario**, not the function:
   `test_returns_none_for_missing`, not `test_get_session`.
4. **Test one thing per test method.** Multi-assertion tests are acceptable only when the
   assertions are logically inseparable (e.g., checking all fields of a returned struct).
5. **Do not test the database via `db._conn()` in route/agent tests.** Tests must interact
   through the public API only (except for setup/teardown bootstrapping).

---

## 8. Live Issues & Outstanding Debt

These are concrete violations found in the current codebase, ordered by severity:

| # | Severity | File | Issue | Target Fix |
|---|---|---|---|---|
| 1 | 🔴 High | `agent/tools.py:9` | `db._conn()` called directly — bypasses the db layer | Add `db.get_order()` helper |
| 2 | 🔴 High | `agent/__init__.py:28-30` | `_ensure_stream_queue` duplicated from `sse_events.py` | Delete copy; import from `sse_events` |
| 3 | 🟠 Medium | `agent/__init__.py:146-159` | Hardcoded tool-name branch for acknowledgement copy | Move copy to `tools.py`; remove branch from orchestrator |
| 4 | 🟠 Medium | `agent/__init__.py`, `sse_events.py` | `thought_queues[sid] = Queue()` in 4+ locations; no `_ensure_thought_queue` helper | Single guard function in `sse_events.py` |
| 5 | 🟠 Medium | `agent/__init__.py:122-123`, `sse_events.py:64-65` | JSON sanitisation duplicated across files | Extract `_sanitize_json_fragment()` helper with unit test |
| 6 | 🟠 Medium | `api/routes/actions.py:57-61` | Raw HTML string returned from `_already_handled` | Move to Jinja2 partial |
| 7 | 🟠 Medium | `api/routes/settings.py:33` | Raw `<span>` HTML returned from settings POST | Move to Jinja2 partial |
| 8 | 🟡 Low | `agent/sse_events.py:19` | `FileSystemLoader("frontend/templates")` uses relative path | Use `Path(__file__)`-relative path |
| 9 | 🟡 Low | `db/__init__.py:113-115` | Bare `except Exception: pass` for migration | Narrow to `sqlite3.OperationalError` |
| 10 | 🟡 Low | `db/__init__.py:275` | `tool_call_id: str = None` — missing `Optional` annotation | `tool_call_id: Optional[str] = None` |
| 11 | 🟡 Low | All `api/routes/` | Route handlers missing return-type annotations and docstrings | Annotate incrementally |
| 12 | 🟡 Low | `poller/__init__.py` | Phase 4 stub not implemented | Implement per §5.5 rules |
| 13 | 🟡 Low | Global | `"RUNNING"` / `"PAUSED"` / `"DONE"` as magic strings | `SessionStatus` Literal/Enum |

---

## 9. Review Checklist (Quick Reference)

Copy this block into every PR description and tick each item before requesting review:

```
### Code Review Checklist

#### Orthogonality
- [ ] No `db._conn()` or raw SQL outside `db/__init__.py`
- [ ] No HTML string construction in Python (use templates)
- [ ] No agent/orchestration logic inside route handlers
- [ ] No duplicate helper functions across modules

#### Simplicity
- [ ] No magic status strings — use `SessionStatus` constants
- [ ] No tool-specific branching in the orchestrator loop
- [ ] No repeated JSON sanitisation logic
- [ ] Smallest change that correctly solves the problem

#### Readability
- [ ] All public functions have type annotations and docstrings
- [ ] Lines ≤ 100 chars; template context dicts extracted into variables
- [ ] Comments explain *why*, not *what*
- [ ] Naming follows conventions table (§4.4)

#### EDA Compliance
- [ ] New SSE events documented in CODE_REVIEW_GUIDELINES.md §5.1 table
- [ ] Any new `emit_*` function lives in `agent/sse_events.py`
- [ ] Cross-thread queue writes use `call_soon_threadsafe`
- [ ] OOB HTML comes from a template, not a format string

#### Testing
- [ ] New public functions have corresponding unit tests
- [ ] Tests are hermetic (no network, no shared state)
- [ ] Test names describe the scenario, not the function name
```

---

*Last updated: 2026-02-21. Maintainer: Engineering Guardian.*
*Update this document whenever a new pattern is established or an outstanding item is resolved.*
