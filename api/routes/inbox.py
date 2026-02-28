from fastapi import APIRouter, Request
from starlette.responses import Response

import db
from api.templates import templates

router = APIRouter()


@router.get("/inbox")
def get_inbox(request: Request) -> Response:
    """Return the full inbox pane with session list and message form."""
    sessions = db.get_all_sessions()
    return templates.TemplateResponse(
        "partials/inbox.html",
        {"request": request, "sessions": sessions},
    )


@router.get("/inbox/sessions")
def get_inbox_sessions(request: Request) -> Response:
    """Return the session-list partial for the 5-second poll refresh."""
    sessions = db.get_all_sessions()
    return templates.TemplateResponse(
        "partials/session_list.html",
        {"request": request, "sessions": sessions},
    )
