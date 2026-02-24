import json
import logging
from pathlib import Path
import db
import poller


def sanitize_json_fragment(raw: str) -> str:
    """Trim trailing garbage after the last '}' in a JSON string.

    Some LLM providers (notably OpenRouter-proxied models) occasionally append
    trailing whitespace or partial tokens after the closing brace of a tool-call
    arguments object. This strips everything after the last valid '}'.
    """
    pos = raw.rfind("}")
    if pos != -1:
        return raw[:pos + 1]
    return raw

log = logging.getLogger("tools")

# ---------------------------------------------------------------------------
# Tools Configuration & Schema
# ---------------------------------------------------------------------------

def check_order_status(order_id: str, customer_phone: str) -> str:
    """Query the orders table and return a human-readable status string.
    Ensures the provided phone number matches the order.
    """
    row = db.get_order(order_id)
    if row is None:
        return "Order {} not found.".format(order_id)
    if row["customer_phone"] != customer_phone:
        return "Order {} not found or phone number does not match.".format(order_id)
    return (
        "Order {order_id}: customer='{customer_name}', "
        "product='{product_name}' x{item_count}, "
        "total=${total_amount:.2f}, status='{status}', "
        "last updated {last_updated}."
    ).format(**row)


def list_orders(customer_phone: str, status: str = "", customer_name: str = "") -> str:
    """Query orders with optional filters and return a summary for the agent to suggest.
    Only returns orders matching the provided customer_phone.
    """
    all_orders = db.get_all_orders()
    # Always filter by the required phone number first
    all_orders = [o for o in all_orders if o["customer_phone"] == customer_phone]
    if status:
        all_orders = [o for o in all_orders if o["status"].lower() == status.lower()]
    if customer_name:
        all_orders = [o for o in all_orders if customer_name.lower() in o["customer_name"].lower()]
    if not all_orders:
        return "No orders found matching the given filters for this phone number."
    lines = []
    for o in all_orders:
        lines.append(
            "- {order_id}: {customer_name} — '{product_name}' x{item_count}, "
            "${total_amount:.2f}, status={status}".format(**o)
        )
    return "Found {} order(s):\n{}".format(len(all_orders), "\n".join(lines))


def issue_refund(order_id: str, amount: float, reason: str, customer_phone: str) -> str:
    """Stub — triggers the HITL gate; only executed after operator approval.
    Validates the phone number matches the order and that the order is not cancelled.
    """
    row = db.get_order(order_id)
    if row is None:
        return "Cannot issue refund: Order {} not found.".format(order_id)
    if row["customer_phone"] != customer_phone:
        return "Cannot issue refund: Phone number does not match order {}.".format(order_id)
    if row["status"].lower() == "cancelled":
        return "Cannot issue refund: Order {} is already cancelled.".format(order_id)
        
    return "Refund of ${:.2f} issued for order {} — reason: {}.".format(
        amount, order_id, reason
    )


def upsert_scheduled_task(
    task_id: str, cron: str, filters: dict, system_prompt_override: str
) -> str:
    """Create or update a polling rule in the scheduledTasks directory."""
    task_dir = Path("scheduledTasks")
    task_dir.mkdir(exist_ok=True)

    file_path = task_dir / "{0}.json".format(task_id)
    task_data = {
        "task_id": task_id,
        "enabled": True,
        "cron": cron,
        "filters": filters,
        "system_prompt_override": system_prompt_override,
    }

    try:
        with open(file_path, "w") as f:
            json.dump(task_data, f, indent=2)
    except PermissionError as e:
        log.error("No write permission for scheduledTasks/: %s", e)
        return "Failed to save task: insufficient permissions to write to scheduledTasks/."
    except OSError as e:
        log.error("OS error writing task %s: %s", task_id, e)
        return "Failed to save task due to filesystem error: {0}".format(e)

    poller.reload_scheduler()
    return "Successfully created/updated task '{0}'. Poller reloaded.".format(task_id)


TOOL_ACK_MESSAGES = {
    "issue_refund": (
        'We acknowledge your refund request for order {order_id} and your '
        'reason for it ("{reason}"). We will escalate this to an agent '
        'to help approve.'
    ),
}

DEFAULT_ACK_MESSAGE = (
    "We acknowledge your request and your reason for it. "
    "We will escalate this to an agent to help approve."
)


def get_ack_message(tool_name: str, args: dict) -> str:
    """Return the HITL acknowledgement message for a given tool call."""
    template = TOOL_ACK_MESSAGES.get(tool_name)
    if template:
        return template.format(**args)
    return DEFAULT_ACK_MESSAGE


SAFE_TOOLS = {
    "check_order_status": check_order_status,
    "list_orders": list_orders,
}
# upsert_scheduled_task has destructive side-effects (fs write + scheduler reload)
# and must pass through the HITL gate before execution.
HITL_TOOLS = {
    "issue_refund": issue_refund,
    "upsert_scheduled_task": upsert_scheduled_task,
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_orders",
            "description": (
                "List all orders in the system with optional filters. "
                "Use this to see what orders exist and suggest them to the user. "
                "Call with no arguments to list all orders, or filter by status or customer name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_phone": {
                        "type": "string",
                        "description": "The customer's phone number as provided by the user.",
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter by order status (e.g. 'processing', 'delayed', 'delivered', 'shipped', 'cancelled'). Leave empty for all.",
                    },
                    "customer_name": {
                        "type": "string",
                        "description": "Filter by customer name (partial match). Leave empty for all.",
                    },
                },
                "required": ["customer_phone"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_order_status",
            "description": "Look up the current status of a customer order. Returns customer name, product, quantity, total amount, status, and last update time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "The order identifier, e.g. ORD-001",
                    },
                    "customer_phone": {
                        "type": "string",
                        "description": "The customer's phone number as provided by the user.",
                    }
                },
                "required": ["order_id", "customer_phone"],
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
                    "customer_phone": {
                        "type": "string",
                        "description": "The customer's phone number as provided by the user.",
                    },
                    "amount": {
                        "type": "number",
                        "description": "Refund amount in USD",
                    },
                    "reason": {"type": "string"},
                },
                "required": ["order_id", "customer_phone", "amount", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upsert_scheduled_task",
            "description": (
                "Create or update an automated CRM polling rule. "
                "Used when the owner wants to automatically message users "
                "matching certain conditions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "A unique, URL-friendly identifier for this task (e.g. 'vip-delays').",
                    },
                    "cron": {
                        "type": "string",
                        "description": "A standard cron expression (e.g., '0 10 * * *' for 10 AM daily).",
                    },
                    "filters": {
                        "type": "object",
                        "description": (
                            "Conditions for matching orders. Supported keys: "
                            "'status' (string), "
                            "'min_hours_since_update' (integer), "
                            "'phone_prefix' (string)."
                        ),
                    },
                    "system_prompt_override": {
                        "type": "string",
                        "description": (
                            "Instructional prompt given when a matching order is found, "
                            "dictating how to message the user."
                        ),
                    },
                },
                "required": ["task_id", "cron", "filters", "system_prompt_override"],
            },
        },
    },
]
