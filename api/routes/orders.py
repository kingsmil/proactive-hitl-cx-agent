from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
import db
from api.templates import templates

router = APIRouter()


@router.get("/orders")
def orders_list(request: Request) -> HTMLResponse:
    """Return an HTML partial containing the list of all orders."""
    orders = db.get_all_orders_with_event_count()
    return templates.TemplateResponse(
        "partials/orders_list.html",
        {"request": request, "orders": orders},
    )


@router.get("/orders/{order_id}/timeline")
def order_timeline(request: Request, order_id: str) -> HTMLResponse:
    """Return an HTML partial showing chronologically ordered events for an order."""
    order = db.get_order(order_id)
    events = db.get_order_timeline(order_id)
    return templates.TemplateResponse(
        "partials/order_timeline.html",
        {"request": request, "order": order, "events": events, "order_id": order_id},
    )
