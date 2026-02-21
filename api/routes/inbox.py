from fastapi import APIRouter, Request
import db
from api.routes.config import templates

router = APIRouter()

@router.get("/inbox")
def get_inbox(request: Request):
    sessions = db.get_all_sessions()
    return templates.TemplateResponse(
        "partials/inbox.html",
        {"request": request, "sessions": sessions},
    )

@router.get("/inbox/sessions")
def get_inbox_sessions(request: Request):
    """Return only the session list partial — used by the 5-second poll
    so the customer-message form is never touched."""
    sessions = db.get_all_sessions()
    return templates.TemplateResponse(
        "partials/session_list.html",
        {"request": request, "sessions": sessions},
    )
