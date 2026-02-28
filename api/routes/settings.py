import os

from fastapi import APIRouter, Request, Form
from starlette.responses import Response

import db
from agent.llm_client import DEFAULT_MODEL
from api.templates import templates

router = APIRouter()


@router.get("/settings")
def get_settings(request: Request) -> Response:
    """Return the settings modal HTML partial."""
    return templates.TemplateResponse(
        "partials/settings_modal.html",
        {
            "request": request,
            "model": db.get_setting("model", DEFAULT_MODEL),
            "has_db_key": bool(db.get_setting("openrouter_api_key", "")),
            "has_env_key": bool(os.environ.get("OPENROUTER_API_KEY", "")),
        },
    )


@router.post("/settings")
async def save_settings(
    request: Request,
    model: str = Form(default=""),
    openrouter_api_key: str = Form(default=""),
) -> Response:
    """Persist updated runtime settings."""
    if model.strip():
        db.set_setting("model", model.strip())
    if openrouter_api_key.strip():
        db.set_setting("openrouter_api_key", openrouter_api_key.strip())
    return templates.TemplateResponse(
        "partials/settings_saved.html", {"request": request}
    )
