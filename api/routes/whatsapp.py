import logging

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import JSONResponse

import db
from agent import run_agent

router = APIRouter()
log = logging.getLogger("whatsapp_webhook")


@router.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    From: str = Form(...),
    Body: str = Form(...),
) -> JSONResponse:
    """Handle inbound WhatsApp messages delivered by a Twilio-style webhook.

    Twilio's ``From`` field carries the ``whatsapp:`` prefix (e.g.
    ``whatsapp:+15551234567``).  We strip it before constructing the session ID
    so the ID is always ``whatsapp:+15551234567`` with exactly one prefix.
    """
    log.info("Received WhatsApp message from %s", From)

    # Normalise phone: strip any "whatsapp:" prefix Twilio may include in From.
    phone = From.strip().replace("whatsapp:", "", 1)
    session_id = "whatsapp:{}".format(phone)

    session = db.get_or_create_session(session_id, channel="whatsapp")
    db.append_message(session_id, "user", Body)
    db.set_session_status(session_id, "RUNNING")

    if session.get("ai_enabled", 1):
        background_tasks.add_task(run_agent, session_id)

    return JSONResponse({"status": "received"})
