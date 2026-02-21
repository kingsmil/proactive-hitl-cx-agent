import db

# ---------------------------------------------------------------------------
# Tools Configuration & Schema
# ---------------------------------------------------------------------------

def check_order_status(order_id: str) -> str:
    """Query the orders table and return a human-readable status string."""
    row = db._conn().execute(
        "SELECT status, last_updated FROM orders WHERE order_id = ?", (order_id,)
    ).fetchone()
    if row is None:
        return "Order {} not found.".format(order_id)
    return "Order {}: status='{}', last updated {}.".format(
        order_id, row["status"], row["last_updated"]
    )


def issue_refund(order_id: str, amount: float, reason: str) -> str:
    """Stub — triggers the HITL gate; only executed after operator approval."""
    return "Refund of ${:.2f} issued for order {} — reason: {}.".format(
        amount, order_id, reason
    )


SAFE_TOOLS = {"check_order_status": check_order_status}
HITL_TOOLS = {"issue_refund": issue_refund}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_order_status",
            "description": "Look up the current status of a customer order.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "The order identifier, e.g. ORD-001",
                    }
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "issue_refund",
            "description": "Issue a full or partial refund to a customer. Requires operator approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "amount": {
                        "type": "number",
                        "description": "Refund amount in USD",
                    },
                    "reason": {"type": "string"},
                },
                "required": ["order_id", "amount", "reason"],
            },
        },
    },
]
