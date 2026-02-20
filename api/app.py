import asyncio
import json
import os
import uuid

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

import db
from agent import DEFAULT_MODEL, run_agent, thought_queues, stream_queues

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="frontend/templates")


@app.on_event("startup")
def startup():
    db.init_db()


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    sessions = db.get_all_sessions()
    if sessions:
        return RedirectResponse(url="/chat/{}".format(sessions[0]["session_id"]))
    return RedirectResponse(url="/chat/{}".format(uuid.uuid4()))


@app.get("/chat/{session_id}")
def chat_page(request: Request, session_id: str):
    session = db.get_or_create_session(session_id)
    pending_count = len(db.get_all_paused_sessions())
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "session_id": session_id,
            "session": session,
            "pending_count": pending_count,
        },
    )


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

@app.post("/chat/message")
async def post_message(
    request: Request,
    background_tasks: BackgroundTasks,
    session_id: str = Form(...),
    message: str = Form(...),
):
    """Legacy alias — routes to customer_message logic."""
    session = db.get_or_create_session(session_id)
    db.append_message(session_id, "user", message)
    db.set_session_status(session_id, "RUNNING")
    if session.get("ai_enabled", 1):
        background_tasks.add_task(run_agent, session_id)
    history = db.get_history(session_id)
    return templates.TemplateResponse(
        "partials/chat_pane.html",
        {
            "request": request,
            "session_id": session_id,
            "session": session,
            "history": history,
            "oob_sessions": db.get_all_sessions(),
        },
    )


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------

@app.get("/inbox")
def get_inbox(request: Request):
    sessions = db.get_all_sessions()
    return templates.TemplateResponse(
        "partials/inbox.html",
        {"request": request, "sessions": sessions},
    )


@app.get("/inbox/sessions")
def get_inbox_sessions(request: Request):
    """Return only the session list partial — used by the 5-second poll
    so the customer-message form is never touched."""
    sessions = db.get_all_sessions()
    return templates.TemplateResponse(
        "partials/session_list.html",
        {"request": request, "sessions": sessions},
    )


# ---------------------------------------------------------------------------
# Session pane (HTMX session switch)
# ---------------------------------------------------------------------------

@app.get("/chat/{session_id}/pane")
def get_chat_pane(request: Request, session_id: str):
    session = db.get_or_create_session(session_id)
    history = db.get_history(session_id)
    return templates.TemplateResponse(
        "partials/chat_pane.html",
        {
            "request": request,
            "session_id": session_id,
            "session": session,
            "history": history,
        },
    )


# ---------------------------------------------------------------------------
# Customer message injection
# ---------------------------------------------------------------------------

@app.post("/chat/customer-message")
async def customer_message(
    request: Request,
    background_tasks: BackgroundTasks,
    session_id: str = Form(default=""),
    message: str = Form(...),
    channel: str = Form(default="web"),
):
    sid = session_id.strip() or str(uuid.uuid4())
    session = db.get_or_create_session(sid, channel)
    db.append_message(sid, "user", message)
    db.set_session_status(sid, "RUNNING")
    if session.get("ai_enabled", 1):
        background_tasks.add_task(run_agent, sid)
    # Switch commune pane to this session
    history = db.get_history(sid)
    return templates.TemplateResponse(
        "partials/chat_pane.html",
        {
            "request": request,
            "session_id": sid,
            "session": db.get_session(sid),
            "history": history,
            "oob_sessions": db.get_all_sessions(),
        },
    )


# ---------------------------------------------------------------------------
# CS Agent manual reply
# ---------------------------------------------------------------------------

@app.post("/chat/agent-reply/{session_id}")
async def agent_reply(
    request: Request,
    session_id: str,
    content: str = Form(...),
):
    db.append_agent_message(session_id, content)
    return templates.TemplateResponse(
        "partials/message_bubble.html",
        {
            "request": request,
            "role": "assistant",
            "content": content,
            "is_manual": True,
        },
    )


# ---------------------------------------------------------------------------
# AI toggle
# ---------------------------------------------------------------------------

@app.post("/sessions/{session_id}/toggle-ai")
async def toggle_ai(
    request: Request,
    session_id: str,
):
    session = db.get_session(session_id)
    if session is None:
        return HTMLResponse("", status_code=404)
    new_state = not bool(session.get("ai_enabled", 1))
    db.set_ai_enabled(session_id, new_state)
    return templates.TemplateResponse(
        "partials/ai_toggle.html",
        {
            "request": request,
            "session_id": session_id,
            "ai_enabled": new_state,
        },
    )


# ---------------------------------------------------------------------------
# SSE — thought stream
# ---------------------------------------------------------------------------

@app.get("/agent/thoughts/{session_id}")
async def thought_stream(request: Request, session_id: str):
    if session_id not in thought_queues:
        thought_queues[session_id] = asyncio.Queue()
    queue = thought_queues[session_id]

    async def generator():
        while True:
            if await request.is_disconnected():
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=15.0)
                # Dicts carry named-event metadata (e.g. {"event": "error", "data": html})
                if isinstance(item, dict):
                    yield item
                else:
                    yield {"data": item}
            except asyncio.TimeoutError:
                yield {"comment": "keepalive"}

    return EventSourceResponse(generator())


# ---------------------------------------------------------------------------
# SSE — reply stream (token-by-token chat streaming)
# ---------------------------------------------------------------------------

@app.get("/chat/stream/{session_id}")
async def chat_stream(request: Request, session_id: str):
    if session_id not in stream_queues:
        stream_queues[session_id] = asyncio.Queue()
    queue = stream_queues[session_id]

    async def generator():
        while True:
            if await request.is_disconnected():
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=15.0)
                if isinstance(item, dict):
                    yield item                      # {"event": "done", "data": html}
                else:
                    yield {"event": "chunk", "data": item}   # plain escaped token
            except asyncio.TimeoutError:
                yield {"comment": "keepalive"}

    return EventSourceResponse(generator())


# ---------------------------------------------------------------------------
# HITL — pending actions
# ---------------------------------------------------------------------------

@app.get("/actions/pending")
def actions_pending(request: Request):
    return templates.TemplateResponse(
        "partials/action_queue.html",
        {"request": request, "pending_sessions": db.get_all_paused_sessions()},
    )


@app.post("/actions/approve/{session_id}")
async def approve_action(
    request: Request,
    background_tasks: BackgroundTasks,
    session_id: str,
):
    # Atomic CAS: PAUSED → RUNNING. Only the first caller wins; concurrent
    # duplicates (double-click, two tabs) get rowcount=0 and are rejected.
    if not db.try_transition_session(session_id, "PAUSED", "RUNNING"):
        return _already_handled(session_id)
    background_tasks.add_task(run_agent, session_id)
    return templates.TemplateResponse(
        "partials/action_decision.html",
        {"request": request, "decision": "approved", "session_id": session_id},
    )


@app.post("/actions/reject/{session_id}")
async def reject_action(
    request: Request,
    background_tasks: BackgroundTasks,
    session_id: str,
    reason: str = Form(default=""),
):
    # Same CAS gate — whichever of approve/reject lands first in the DB wins.
    if not db.try_transition_session(session_id, "PAUSED", "RUNNING"):
        return _already_handled(session_id)
    if reason.strip():
        rejection_msg = "Action rejected by operator. Reason: {}".format(reason.strip())
    else:
        rejection_msg = "Action rejected by operator."
    db.delete_pending_action(session_id)
    db.append_message(session_id, "tool", rejection_msg)
    background_tasks.add_task(run_agent, session_id)
    return templates.TemplateResponse(
        "partials/action_decision.html",
        {"request": request, "decision": "rejected", "session_id": session_id},
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.get("/settings")
def get_settings(request: Request):
    return templates.TemplateResponse(
        "partials/settings_modal.html",
        {
            "request":     request,
            "model":       db.get_setting("model", DEFAULT_MODEL),
            "has_db_key":  bool(db.get_setting("openrouter_api_key", "")),
            "has_env_key": bool(os.environ.get("OPENROUTER_API_KEY", "")),
        },
    )


@app.post("/settings")
async def save_settings(
    request: Request,
    model:              str = Form(default=""),
    openrouter_api_key: str = Form(default=""),
):
    if model.strip():
        db.set_setting("model", model.strip())
    if openrouter_api_key.strip():
        db.set_setting("openrouter_api_key", openrouter_api_key.strip())
    return HTMLResponse(
        '<span class="chip chip-sage" style="animation:driftIn .3s both;">Sealed ✦</span>'
    )


def _already_handled(session_id: str) -> HTMLResponse:
    """Returned when an approve/reject arrives after the action was already resolved."""
    return HTMLResponse(
        '<div class="action-card result-rejected" style="opacity:0.6;">'
        "⚠ Already handled by another operator — "
        "<code style=\"font-size:9px;\">{}</code></div>".format(session_id)
    )
