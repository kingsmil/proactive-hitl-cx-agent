# Codebase Refactoring Plan

## Phase 1 Findings
The core application currently implements an HTMX-driven dashboard, a FastAPI backend, and a custom LLM orchestration loop for a Human-in-the-Loop customer service agent. While functional, there are several areas where the codebase lacks modularity and relies on patterns that reduce readability and make iteration difficult for a junior engineer.

## Proposed Refactors (Focus: Simplicity & Readability)

### 1. Split the Agent Monolith (`agent/__init__.py`) - [x] DONE
- **Current State:** `agent/__init__.py` is a 566-line monolith handling HTTP requests to LLMs, retry logic, tool definitions, Server-Sent Events (SSE) logic, HTML rendering, and the primary orchestration loop.
- **Why refactor:** Mixing transport logic, database side effects, and purely algorithmic orchestration makes the code extremely difficult to follow or test in isolation. A junior engineer won't easily see where the "brain" stops and the "rendering/transport" starts.
- **Action Items:**
  - Move HTTP client logic (`call_llm`, `call_llm_streaming`, payload builders, chunk parsers) to `agent/llm_client.py`. (Done)
  - Move tool definitions (`check_order_status`, `issue_refund`, and `TOOLS` schema) to `agent/tools.py`. (Done)
  - Move SSE event emitting functions (`emit_thought`, `emit_error`, etc.) to `agent/sse_events.py` (or similar). (Done)
  - Keep only the core logic (`run_agent` and `_run_agent_body`) in `agent/orchestrator.py` or the `__init__.py`. (Done)
- **Testing Strategy:** Browser Agent Testing. Since this breaks up complex async streaming logic, we need to verify the UI still streams thought and token processes seamlessly using `http://127.0.0.1:8000/`.

### 2. Eliminate Hardcoded HTML from Python Files - [x] DONE
- **Current State:** Functions like `emit_chat_append`, `emit_stream_done`, and `emit_stream_error` in `agent/__init__.py` return manually concatenated string HTML with inline styling (e.g., `'<div class="msg-agent...</div>'`).
- **Why refactor:** Hardcoded HTML in Python breaks IDE syntax highlighting, is prone to typo-related syntax errors, and forces UI adjustments into the backend flow. It violates the separation of concerns.
- **Action Items:** 
  - Create new `.html` files in `frontend/templates/partials/` (e.g. `agent_bubble.html`, `agent_bubble_done.html`). (Done)
  - Update the `agent` SSE emitting methods to invoke `_jinja.get_template(...).render(...)` instead of concatenating strings, matching the `emit_thought` pattern. (Done)
- **Testing Strategy:** Browser Agent Testing. We will interact with the chat interface and inspect the DOM directly to ensure the raw HTML rendering behaves identically to before.

### 3. Add `TypedDict` Signatures to Database Returns (`db/__init__.py`) - [x] DONE
- **Current State:** The database returns generic Python dictionaries like `return dict(row)`.
- **Why refactor:** When querying `db.get_session(sid)`, a junior engineer has no IDE autocomplete or guarantee of what fields exist (e.g., does it use `"status"` or `"state"`?).
- **Action Items:** 
  - Import `TypedDict` from typing. (Done)
  - Define `SessionRow`, `OrderRow`, and `PendingActionRow`. (Done)
  - Update return annotations like `-> Optional[Dict]` to `-> Optional[SessionRow]`. (Done)
- **Testing Strategy:** Unit Testing via `tests/test_db.py`. Since we are not altering logic but just adding type hints to Python function signatures, running `python -m unittest tests/test_db.py` will guarantee no regressions. (Done - 45 tests ran in 0.129s)

### 4. Organize API Routes (`api/app.py`) - [x] DONE
- **Current State:** All routes (pages, API, settings, chat streams, inbox polling, HITL validations) are in one 350-line file.
- **Why refactor:** As new features are added (like the pending WhatsApp channels or Webhooks), `app.py` will rapidly expand out of control.
- **Action Items:** 
  - Leverage `APIRouter` to split `app.py` into distinct functional route files: `api/routes/chat.py`, `api/routes/actions.py`, `api/routes/sse.py`, `api/routes/settings.py`, `api/routes/inbox.py`. (Done)
- **Testing Strategy:** Verify API loads completely without import errors, verified in Python shell directly using loaded paths. (Done)

## Execution Order
1. Implement DB Typings (Lowest risk, high reward for downstream work).
2. Extract hardcoded HTML into templates.
3. Split the Agent Monolith.
4. Route Reorganization (if time permits). 

*(Note: The poller and WhatsApp pieces are not complete as per `claude.md`, so we will not touch them.)*
