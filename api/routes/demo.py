import asyncio
import logging

from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse

import db
from agent import run_agent

router = APIRouter()
log = logging.getLogger("demo")


async def _trigger_outreach_for_order(order: dict) -> str:
    """Create a proactive session for a single order and enqueue the agent."""
    sid = "proactive-demo-{}".format(order["order_id"])
    db.get_or_create_session(sid, channel="proactive")

    # Log the outreach trigger as an order event (visible in the event log timeline)
    outreach_desc = (
        "Proactive outreach initiated — order {order_id} for {customer_name} "
        "({customer_phone}), product '{product_name}', total ${total_amount:.2f}, "
        "status '{status}'."
    ).format(**order)
    db.log_order_event(
        order["order_id"],
        "outreach_triggered",
        outreach_desc,
        actor="system",
        session_id=sid,
    )

    # Inject as a system-level instruction (not a "user" message).
    # This gives the LLM context without rendering as a customer bubble in the chat.
    system_instruction = (
        "The customer's order has been delayed. Reach out empathetically, "
        "provide a clear explanation, and proactively offer a 10% discount or "
        "refund as compensation. Keep the tone warm, professional, and concise.\n"
        "Context: Order {order_id} for {customer_name} ({customer_phone}) — "
        "product '{product_name}', total ${total_amount:.2f}, status '{status}'."
    ).format(**order)

    db.append_raw_message(sid, {
        "role": "system",
        "content": system_instruction,
    })

    db.set_session_status(sid, db.RUNNING)
    asyncio.create_task(run_agent(sid))
    return sid


@router.post("/demo/trigger-outreach")
async def demo_trigger_outreach(
    phone: str = Form(default=""),
) -> JSONResponse:
    """Force-trigger proactive outreach for delayed orders (demo use)."""
    filters = {"status": "delayed"}
    if phone.strip():
        filters["phone_prefix"] = phone.strip()

    # Reset outreached flag so orders can be re-triggered during demos
    all_orders = db.get_all_orders()
    for o in all_orders:
        if o["status"] == "delayed" and o.get("outreached", 0):
            if (
                not phone.strip()
                or o["customer_phone"].startswith(phone.strip())
            ):
                db.mark_order_not_outreached(o["order_id"])

    orders = db.query_orders_by_filters(filters)
    if not orders:
        return JSONResponse({
            "status": "no_orders",
            "message": "No delayed orders found matching filters.",
        })

    triggered = []
    for order in orders:
        db.mark_order_outreached(order["order_id"])
        sid = await _trigger_outreach_for_order(order)
        triggered.append({
            "session_id": sid,
            "order_id": order["order_id"],
            "phone": order["customer_phone"],
        })
        log.info(
            "Demo outreach triggered for %s -> session %s",
            order["order_id"], sid,
        )

    return JSONResponse({
        "status": "triggered",
        "count": len(triggered),
        "sessions": triggered,
    })
