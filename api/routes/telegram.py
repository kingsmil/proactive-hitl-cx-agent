import asyncio
import logging
import os
import secrets
from typing import Any, Dict

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

import db
from agent import run_agent
from agent.sse_events import emit_user_message

router = APIRouter()
log = logging.getLogger("telegram_webhook")

_WEBHOOK_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def _verify_secret(request: Request) -> bool:
    """Return False if a webhook secret is configured and the request header does not match."""
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if not expected:
        # Secret not configured — skip enforcement (dev mode).
        return True
    received = request.headers.get(_WEBHOOK_SECRET_HEADER, "")
    # Constant-time comparison prevents timing oracle attacks.
    return secrets.compare_digest(expected, received)


@router.post("/webhook/telegram")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    """Handle inbound Telegram updates.

    Validates the X-Telegram-Bot-Api-Secret-Token header before processing.
    Telegram sends updates as JSON; we handle 'message' updates containing 'text'.
    The session ID is constructed as 'telegram:{chat_id}'.
    """
    if not _verify_secret(request):
        log.warning("Rejected Telegram webhook — invalid secret token")
        return JSONResponse({"status": "forbidden"}, status_code=403)

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
    db.set_session_status(session_id, db.RUNNING)

    # Push user message to SSE so the operator's chat pane updates in real-time
    await emit_user_message(session_id, text)

    # Trigger the agent if AI is enabled for this session
    if session.get("ai_enabled", 1):
        background_tasks.add_task(run_agent, session_id)

    return JSONResponse({"status": "received"})
