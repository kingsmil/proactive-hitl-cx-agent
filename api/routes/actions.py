from fastapi import APIRouter, Request, BackgroundTasks, Form
from starlette.responses import Response

import db
from agent import run_agent
from api.templates import templates

router = APIRouter()


@router.get("/actions/pending")
def actions_pending(request: Request) -> Response:
    """Return the pending approvals queue HTML partial."""
    return templates.TemplateResponse(
        "partials/action_queue.html",
        {"request": request, "pending_sessions": db.get_all_paused_sessions()},
    )


@router.post("/actions/approve/{session_id}")
async def approve_action(
    request: Request,
    background_tasks: BackgroundTasks,
    session_id: str,
) -> Response:
    """Grant operator approval for a pending HITL action."""
    # Atomic CAS: PAUSED -> RUNNING. Only the first caller wins; concurrent
    # duplicates (double-click, two tabs) get rowcount=0 and are rejected.
    if not db.try_transition_session(session_id, db.PAUSED, db.RUNNING):
        return _already_handled(request, session_id)
    pending = db.get_pending_action(session_id)
    if pending and pending.get("tool_name") == "issue_refund":
        order_id = pending["arguments"].get("order_id", "")
        if order_id:
            db.log_order_event(
                order_id,
                "refund_approved",
                "Operator approved refund",
                actor="operator",
                session_id=session_id,
            )
    background_tasks.add_task(run_agent, session_id)
    pending_count = max(0, len(db.get_all_paused_sessions()) - 1)
    ctx = {
        "request": request,
        "decision": "approved",
        "session_id": session_id,
        "pending_count": pending_count,
    }
    return templates.TemplateResponse(
        "partials/action_decision.html", ctx
    )


@router.post("/actions/reject/{session_id}")
async def reject_action(
    request: Request,
    background_tasks: BackgroundTasks,
    session_id: str,
    reason: str = Form(default=""),
) -> Response:
    """Deny a pending HITL action, optionally with a reason."""
    # Same CAS gate -- whichever of approve/reject lands first in the DB wins.
    if not db.try_transition_session(session_id, db.PAUSED, db.RUNNING):
        return _already_handled(request, session_id)
    pending = db.get_pending_action(session_id)
    if pending and pending.get("tool_name") == "issue_refund":
        order_id = pending["arguments"].get("order_id", "")
        if order_id:
            db.log_order_event(
                order_id,
                "refund_rejected",
                "Operator rejected refund",
                actor="operator",
                session_id=session_id,
            )
    if reason.strip():
        rejection_msg = "Action rejected by operator. Reason: {}".format(
            reason.strip()
        )
    else:
        rejection_msg = "Action rejected by operator."
    db.delete_pending_action(session_id)
    db.append_message(session_id, "tool", rejection_msg)
    background_tasks.add_task(run_agent, session_id)
    ctx = {
        "request": request,
        "decision": "rejected",
        "session_id": session_id,
        "pending_count": len(db.get_all_paused_sessions()),
    }
    return templates.TemplateResponse(
        "partials/action_decision.html", ctx
    )


def _already_handled(request: Request, session_id: str) -> Response:
    """Return a stale-action notice when the action was already resolved."""
    return templates.TemplateResponse(
        "partials/action_already_handled.html",
        {"request": request, "session_id": session_id},
    )
