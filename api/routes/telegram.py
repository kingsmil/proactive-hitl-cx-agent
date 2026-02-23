import logging
from typing import Any, Dict

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

import db
from agent import run_agent

router = APIRouter()
log = logging.getLogger("telegram_webhook")


@router.post("/webhook/telegram")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    """Handle inbound Telegram updates.

    Telegram sends updates as JSON. We care about 'message' updates containing 'text'.
    The session ID is constructed as 'telegram:{chat_id}'.
    """
    data: Dict[str, Any] = await request.json()
    log.info("Received Telegram update: %s", data)

    message = data.get("message")
    if not message:
        return JSONResponse({"status": "ignored", "reason": "no_message"})

    chat = message.get("chat")
    text = message.get("text")

    if not chat or not text:
        return JSONResponse({"status": "ignored", "reason": "missing_chat_or_text"})

    chat_id = str(chat.get("id"))
    session_id = "telegram:{}".format(chat_id)

    # Ensure the session exists and record the message
    session = db.get_or_create_session(session_id, channel="telegram")
    db.append_message(session_id, "user", text)
    db.set_session_status(session_id, "RUNNING")

    # Trigger the agent if AI is enabled for this session
    if session.get("ai_enabled", 1):
        background_tasks.add_task(run_agent, session_id)

    return JSONResponse({"status": "received"})
