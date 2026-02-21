import json
import logging
from pathlib import Path
import db
import poller

log = logging.getLogger("tools")

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


def upsert_scheduled_task(task_id: str, cron: str, filters: dict, system_prompt_override: str) -> str:
    """Creates a new polling rule."""
    task_dir = Path("scheduledTasks")
    task_dir.mkdir(exist_ok=True)
    
    file_path = task_dir / f"{task_id}.json"
    
    task_data = {
        "task_id": task_id,
        "enabled": True,
        "cron": cron,
        "filters": filters,
        "system_prompt_override": system_prompt_override
    }
    
    try:
        with open(file_path, "w") as f:
            json.dump(task_data, f, indent=2)
            
        poller.reload_scheduler()
        return f"Successfully created/updated task '{task_id}'. Poller reloaded."
    except Exception as e:
        log.error(f"Failed to upsert task {task_id}: {e}")
        return f"Failed to save task: {e}"


SAFE_TOOLS = {
    "check_order_status": check_order_status,
    "upsert_scheduled_task": upsert_scheduled_task
}
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
    {
        "type": "function",
        "function": {
            "name": "upsert_scheduled_task",
            "description": "Create or update an automated CRM polling rule. Used when the owner wants to automatically message users matching certain conditions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "A unique, URL-friendly identifier for this task (e.g. 'vip-delays')."
                    },
                    "cron": {
                        "type": "string",
                        "description": "A standard cron expression dictating when this runs (e.g., '0 10 * * *' for 10 AM daily)."
                    },
                    "filters": {
                        "type": "object",
                        "description": "Conditions for matching orders. Supported keys: 'status' (string, e.g. 'delayed'), 'min_hours_since_update' (integer, e.g. 24), 'phone_prefix' (string, e.g. '+1-555')."
                    },
                    "system_prompt_override": {
                        "type": "string",
                        "description": "The exact instructional prompt you will be given when a matching order is found, dictating how to message the user."
                    }
                },
                "required": ["task_id", "cron", "filters", "system_prompt_override"]
            }
        }
    }
]
