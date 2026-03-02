import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

import db
from agent.rules_ai import run_rules_ai
from api.templates import templates

router = APIRouter()
log = logging.getLogger("rules")


@router.get("/rules", response_class=HTMLResponse)
async def rules_panel(request: Request):
    """Full rules panel — task list + chat history."""
    tasks = db.list_scheduled_tasks()
    chat_messages = db.get_rules_chat_display()
    return templates.TemplateResponse(
        "partials/rules_panel.html",
        {
            "request": request,
            "tasks": tasks,
            "chat_messages": chat_messages,
        },
    )


@router.get("/rules/list", response_class=HTMLResponse)
async def rules_list(request: Request):
    """Just the rules list partial (for OOB refresh)."""
    tasks = db.list_scheduled_tasks()
    return templates.TemplateResponse(
        "partials/rules_list.html",
        {"request": request, "tasks": tasks},
    )


@router.post("/rules/chat", response_class=HTMLResponse)
async def rules_chat(request: Request, message: str = Form(...)):
    """Send a message to the Rules AI and return user + AI bubbles."""
    ai_reply = await run_rules_ai(message)
    tasks = db.list_scheduled_tasks()
    return templates.TemplateResponse(
        "partials/rules_chat_response.html",
        {
            "request": request,
            "user_message": message,
            "ai_reply": ai_reply,
            "tasks": tasks,
        },
    )


@router.post("/rules/chat/clear", response_class=HTMLResponse)
async def rules_chat_clear(request: Request):
    """Clear rules chat history and return empty state."""
    db.clear_rules_chat_history()
    return templates.TemplateResponse(
        "partials/rules_chat_area.html",
        {"request": request},
    )
