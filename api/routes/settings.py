import os
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
import db
from agent.llm_client import DEFAULT_MODEL
from api.routes.config import templates

router = APIRouter()

@router.get("/settings")
def get_settings(request: Request):
    return templates.TemplateResponse(
        "partials/settings_modal.html",
        {
            "request":     request,
            "model":       db.get_setting("model", DEFAULT_MODEL),
            "has_db_key":  bool(db.get_setting("openrouter_api_key", "")),
            "has_env_key": bool(os.environ.get("OPENROUTER_API_KEY", "")),
        },
    )

@router.post("/settings")
async def save_settings(
    request: Request,
    model: str = Form(default=""),
    openrouter_api_key: str = Form(default=""),
) -> HTMLResponse:
    """Save the settings and return a brief seal animation."""
    if model.strip():
        db.set_setting("model", model.strip())
    if openrouter_api_key.strip():
        db.set_setting("openrouter_api_key", openrouter_api_key.strip())
    return templates.TemplateResponse(
        "partials/settings_sealed.html",
        {"request": request}
    )
