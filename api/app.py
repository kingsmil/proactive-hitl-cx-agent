import asyncio
import uuid

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

import db
from agent import run_agent, thought_queues

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
    return RedirectResponse(url="/chat/{}".format(uuid.uuid4()))


@app.get("/chat/{session_id}")
def chat_page(request: Request, session_id: str):
    db.get_or_create_session(session_id)
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "session_id": session_id},
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
    session = db.get_or_create_session(session_id)
    db.append_message(session_id, "user", message)
    db.set_session_status(session_id, "RUNNING")
    background_tasks.add_task(run_agent, session_id)
    return templates.TemplateResponse(
        "partials/chat_exchange.html",
        {
            "request": request,
            "user_message": message,
            "channel": session["channel"],
            "agent_message": "",
            "pending": True,
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
                data = await asyncio.wait_for(queue.get(), timeout=15.0)
                yield {"data": data}
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
    db.set_session_status(session_id, "RUNNING")
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
    if reason.strip():
        rejection_msg = "Action rejected by operator. Reason: {}".format(reason.strip())
    else:
        rejection_msg = "Action rejected by operator."
    db.delete_pending_action(session_id)
    db.append_message(session_id, "tool", rejection_msg)
    db.set_session_status(session_id, "RUNNING")
    background_tasks.add_task(run_agent, session_id)
    return templates.TemplateResponse(
        "partials/action_decision.html",
        {"request": request, "decision": "rejected", "session_id": session_id},
    )
