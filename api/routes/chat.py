import uuid
from fastapi import APIRouter, Request, Form, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse
import db
from agent import run_agent
from api.routes.config import templates

router = APIRouter()

@router.get("/")
def root():
    sessions = db.get_all_sessions()
    if sessions:
        # Prefer the most active session: RUNNING > PAUSED > DONE
        priority = {"RUNNING": 0, "PAUSED": 1, "DONE": 2}
        best = min(sessions, key=lambda s: priority.get(s.get("status", "DONE"), 2))
        return RedirectResponse(url="/chat/{}".format(best["session_id"]))
    return RedirectResponse(url="/chat/{}".format(uuid.uuid4()))

@router.get("/chat/{session_id}")
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

@router.post("/chat/message")
async def post_message(
    request: Request,
    background_tasks: BackgroundTasks,
    session_id: str = Form(...),
    message: str = Form(...),
):
    """Legacy alias — routes to customer_message logic."""
    session = db.get_or_create_session(session_id)
    db.append_message(session_id, "user", message)
    db.set_session_status(session_id, db.RUNNING)
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

@router.get("/chat/{session_id}/pane")
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

@router.post("/chat/customer-message")
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
    db.set_session_status(sid, db.RUNNING)
    if session.get("ai_enabled", 1):
        background_tasks.add_task(run_agent, sid)
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

@router.post("/chat/agent-reply/{session_id}")
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

@router.post("/sessions/{session_id}/toggle-ai")
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

@router.get("/chat/{session_id}/event-log")
def get_event_log(request: Request, session_id: str) -> HTMLResponse:
    orders = db.get_session_orders(session_id)
    events = []
    seen_events = set()
    for oid in orders:
        order_events = db.get_order_timeline(oid)
        for ev in order_events:
            if ev["event_id"] not in seen_events:
                events.append(ev)
                seen_events.add(ev["event_id"])
                
    history = db.get_history(session_id)
    session = db.get_session(session_id)
    base_ts = session.get("created_at", "1970-01-01T00:00:00+00:00") if session else "1970-01-01T00:00:00+00:00"
    
    for i, msg in enumerate(history):
        role = msg.get("role")
        if role in ("user", "assistant"):
            content = msg.get("content")
            if not content:
                continue
            event_type = "user_message" if role == "user" else "agent_reply"
            actor = "user" if role == "user" else ("operator" if msg.get("is_manual") else "agent")
            ts = msg.get("timestamp") or base_ts
            
            # Map multiple orders visually, or just session.
            first_order = list(orders)[0] if orders else "Session"
            events.append({
                "event_id": f"msg_{i}",
                "order_id": first_order,
                "event_type": event_type,
                "description": content,
                "actor": actor,
                "created_at": ts
            })
    
    events.sort(key=lambda e: e["created_at"])
    
    return templates.TemplateResponse(
        "partials/event_log.html",
        {"request": request, "session_id": session_id, "events": events}
    )
