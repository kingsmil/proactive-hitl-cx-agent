"""Rules AI — natural-language configuration of outreach rules / scheduled tasks.

The operator talks to an AI assistant that can create, update, delete, toggle,
and list outreach rules.  All tool calls are executed immediately (no HITL gate)
because the operator IS the authority.
"""

import json
import logging

from apscheduler.triggers.cron import CronTrigger

import db
from agent.llm_client import call_llm_with_custom_prompt

log = logging.getLogger("rules_ai")

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

RULES_SYSTEM_PROMPT = """\
You are the Rules Configuration Assistant for CustomerClaw, an e-commerce customer support platform.

Your job is to help operators create, update, delete, and manage automated outreach rules.
Each rule is a scheduled task that periodically queries the order database for matching orders
and triggers proactive outreach to affected customers.

## Rule Structure
Each rule has:
- **task_id**: A unique snake_case identifier (e.g. "delayed_order_followup")
- **enabled**: Whether the rule is active (true/false)
- **cron**: A cron expression for scheduling (5-field: minute hour day-of-month month day-of-week)
- **filters**: Query filters to match orders (all optional — omit a field to not filter on it):
  - `status`: Order status(es) to match — a single string or a list of strings. Valid values: processing, delayed, shipped, delivered, cancelled, refunded. **Omit entirely to match ALL statuses.**
  - `exclude_statuses`: List of statuses to exclude (e.g. ["cancelled", "refunded"])
  - `min_hours_since_update`: Only orders not updated in this many hours
  - `phone_prefix`: Only orders with phone numbers starting with this prefix
  - `include_outreached`: If true, include orders that have already been contacted. Default is false (only contact new orders).
- **system_prompt_override**: Instructions for the AI agent when reaching out to matched customers

## Cron Syntax Quick Reference
- `* * * * *` = every minute
- `0 * * * *` = every hour
- `0 10 * * *` = daily at 10:00 AM
- `*/30 * * * *` = every 30 minutes
- `0 */2 * * *` = every 2 hours
- `0 9-17 * * 1-5` = hourly 9am-5pm weekdays

**IMPORTANT — Timezone:** Cron expressions run in the **server's local timezone**
(detected automatically by APScheduler via tzlocal). Do NOT convert to UTC.
If the operator says "12:24 AM", use `24 0 * * *` directly — the hour field is
local time (0 = midnight, 16 = 4 PM).

## Guidelines
- Always use list_rules first to understand what already exists before making changes.
- When creating rules, suggest sensible defaults and explain your choices.
- Validate cron expressions before saving.
- Use clear, descriptive task_id names in snake_case.
- For the system_prompt_override, write empathetic, professional outreach instructions.
- Be concise in your responses — operators are busy.
"""

# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format)
# ---------------------------------------------------------------------------

RULES_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_rules",
            "description": "List all configured outreach rules with their status and settings.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_rule",
            "description": "Get the full configuration of a specific rule.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The rule identifier.",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_or_update_rule",
            "description": "Create a new outreach rule or update an existing one.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Unique snake_case identifier for the rule.",
                    },
                    "cron": {
                        "type": "string",
                        "description": "Cron expression (5-field) for scheduling.",
                    },
                    "filters": {
                        "type": "object",
                        "description": "Order query filters. Omit 'status' to match ALL order statuses.",
                        "properties": {
                            "status": {
                                "oneOf": [
                                    {"type": "string", "description": "Single status to match."},
                                    {"type": "array", "items": {"type": "string"}, "description": "List of statuses to match."},
                                ],
                                "description": "Order status(es) to match: processing, delayed, shipped, delivered, cancelled, refunded. Omit to match all.",
                            },
                            "exclude_statuses": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Statuses to exclude (e.g. ['cancelled', 'refunded']).",
                            },
                            "min_hours_since_update": {"type": "number", "description": "Only orders not updated in this many hours."},
                            "phone_prefix": {"type": "string", "description": "Only orders with phone numbers starting with this prefix."},
                            "include_outreached": {"type": "boolean", "description": "If true, include orders already contacted. Default false."},
                        },
                    },
                    "system_prompt_override": {
                        "type": "string",
                        "description": "Instructions for the AI agent during outreach.",
                    },
                    "enabled": {
                        "type": "boolean",
                        "description": "Whether the rule should be active.",
                    },
                },
                "required": ["task_id", "cron", "filters", "system_prompt_override", "enabled"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_rule",
            "description": "Permanently delete an outreach rule.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The rule identifier to delete.",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "toggle_rule",
            "description": "Enable or disable an outreach rule without deleting it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The rule identifier.",
                    },
                    "enabled": {
                        "type": "boolean",
                        "description": "True to enable, false to disable.",
                    },
                },
                "required": ["task_id", "enabled"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _exec_list_rules() -> str:
    tasks = db.list_scheduled_tasks()
    if not tasks:
        return "No rules configured yet."
    lines = []
    for t in tasks:
        status = "enabled" if t.get("enabled") else "disabled"
        filters_str = json.dumps(t.get("filters", {}))
        lines.append(
            f"- {t['task_id']}: cron='{t.get('cron', '?')}', status={status}, filters={filters_str}"
        )
    return "\n".join(lines)


def _exec_get_rule(task_id: str) -> str:
    task = db.get_scheduled_task(task_id)
    if task is None:
        return f"Rule '{task_id}' not found."
    return json.dumps(task, indent=2)


def _exec_create_or_update_rule(
    task_id: str,
    cron: str,
    filters: dict,
    system_prompt_override: str,
    enabled: bool,
) -> str:
    # Validate cron expression
    try:
        CronTrigger.from_crontab(cron)
    except (ValueError, KeyError) as e:
        return f"Invalid cron expression '{cron}': {e}"

    is_update = db.get_scheduled_task(task_id) is not None

    task = {
        "task_id": task_id,
        "enabled": enabled,
        "cron": cron,
        "filters": filters,
        "system_prompt_override": system_prompt_override,
    }
    db.save_scheduled_task(task)

    # Reload the scheduler to pick up the change
    try:
        from poller import reload_scheduler
        reload_scheduler()
    except (ImportError, OSError, ValueError) as e:
        log.warning("Failed to reload scheduler: %s", e)

    action = "updated" if is_update else "created"
    return f"Rule '{task_id}' {action} successfully. Scheduler reloaded."


def _exec_delete_rule(task_id: str) -> str:
    deleted = db.delete_scheduled_task(task_id)
    if not deleted:
        return f"Rule '{task_id}' not found."
    try:
        from poller import reload_scheduler
        reload_scheduler()
    except (ImportError, OSError, ValueError) as e:
        log.warning("Failed to reload scheduler: %s", e)
    return f"Rule '{task_id}' deleted. Scheduler reloaded."


def _exec_toggle_rule(task_id: str, enabled: bool) -> str:
    task = db.toggle_scheduled_task(task_id, enabled)
    if task is None:
        return f"Rule '{task_id}' not found."
    try:
        from poller import reload_scheduler
        reload_scheduler()
    except (ImportError, OSError, ValueError) as e:
        log.warning("Failed to reload scheduler: %s", e)
    status = "enabled" if enabled else "disabled"
    return f"Rule '{task_id}' is now {status}. Scheduler reloaded."


_TOOL_DISPATCH = {
    "list_rules": lambda args: _exec_list_rules(),
    "get_rule": lambda args: _exec_get_rule(**args),
    "create_or_update_rule": lambda args: _exec_create_or_update_rule(**args),
    "delete_rule": lambda args: _exec_delete_rule(**args),
    "toggle_rule": lambda args: _exec_toggle_rule(**args),
}

# ---------------------------------------------------------------------------
# Orchestrator loop
# ---------------------------------------------------------------------------

MAX_ITERATIONS = 10


async def run_rules_ai(user_message: str) -> str:
    """Process a user message through the Rules AI and return the final text reply.

    Appends all messages (user, assistant, tool) to the rules_chat DB table so
    history is preserved across calls.
    """
    # Persist user message
    db.append_rules_chat_message("user", user_message)

    for _ in range(MAX_ITERATIONS):
        history = db.get_rules_chat_history()
        response = call_llm_with_custom_prompt(RULES_SYSTEM_PROMPT, history, RULES_TOOLS)

        choice = response["choices"][0]
        msg = choice.get("message", {})
        finish_reason = choice.get("finish_reason", "stop")

        if finish_reason == "tool_calls" or msg.get("tool_calls"):
            # Persist assistant message with tool_calls
            db.append_rules_chat_message(
                "assistant",
                msg.get("content") or "",
                tool_calls=json.dumps(msg["tool_calls"]),
            )

            # Execute each tool call
            for tc in msg["tool_calls"]:
                fn_name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except (json.JSONDecodeError, TypeError):
                    args = {}

                handler = _TOOL_DISPATCH.get(fn_name)
                if handler:
                    result = handler(args)
                else:
                    result = f"Unknown tool: {fn_name}"

                log.info("Rules AI tool %s(%s) -> %s", fn_name, args, result[:200])

                # Persist tool result
                db.append_rules_chat_message(
                    "tool",
                    result,
                    tool_call_id=tc.get("id", ""),
                )

            # Continue loop to let the LLM process tool results
            continue

        else:
            # Final text reply
            reply = msg.get("content") or ""
            db.append_rules_chat_message("assistant", reply)
            return reply

    # Safety: max iterations reached
    fallback = "I've reached the maximum number of steps. Please try again with a simpler request."
    db.append_rules_chat_message("assistant", fallback)
    return fallback
