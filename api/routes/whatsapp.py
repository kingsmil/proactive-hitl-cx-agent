from fastapi import APIRouter, Request, BackgroundTasks, Form, HTTPException
import logging
import db
from agent import run_agent

router = APIRouter()
log = logging.getLogger("whatsapp_webhook")

@router.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    From: str = Form(...),
    Body: str = Form(...)
):
    """
    Handles inbound messages from WhatsApp via a Twilio-style webhook.
    Form data `From` contains the sender's phone number.
    Form data `Body` contains the message text.
    """
    log.info("Received WhatsApp message from %s: %s", From, Body)
    
    # Use the phone number as the session ID directly
    session_id = f"whatsapp:{From.strip()}"
    
    # Get or create the session for this phone number, explicitly marked as 'whatsapp' channel
    session = db.get_or_create_session(session_id, channel="whatsapp")
    
    # Append the incoming message from the customer
    db.append_message(session_id, "user", Body)
    
    # Put the session into RUNNING state
    db.set_session_status(session_id, "RUNNING")
    
    # If the AI is enabled for this session, kick off the agent in the background
    if session.get("ai_enabled", 1):
        background_tasks.add_task(run_agent, session_id)
        
    # Return a basic TwiML-like or 200 OK response
    return {"status": "received"}
