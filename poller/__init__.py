import asyncio
import json
import logging
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import db

log = logging.getLogger("poller")

scheduler = AsyncIOScheduler()
TASKS_DIR = Path("scheduledTasks")


async def execute_task(task: dict) -> None:
    """Fire when a cron trigger matches: find matching orders and queue agent sessions."""
    if not task.get("enabled", False):
        return

    stale_orders = db.query_orders_by_filters(task.get("filters", {}))
    if not stale_orders:
        return

    log.info("Task '%s' found %d applicable orders.", task.get("task_id"), len(stale_orders))

    # Lazy import to avoid a circular dependency (agent → tools → poller → agent).
    from agent import run_agent  # noqa: PLC0415

    for order in stale_orders:
        # Mark before dispatching to prevent duplicate triggers on re-entry.
        db.mark_order_outreached(order["order_id"])
        db.log_order_event(
            order["order_id"], "outreach_triggered",
            "Proactive outreach triggered by rule '{}'".format(task.get("task_id", "unknown")),
            actor="poller",
        )

        task_id = task.get("task_id", "unknown")
        sid = f"proactive-{task_id}-{order['order_id']}"
        db.get_or_create_session(sid, channel="proactive")

        # Inject as a system-level instruction (not "user") so it doesn't
        # render as a customer bubble in the chat pane.
        customer_name = order.get('customer_name', 'Customer')
        system_instruction = (
            f"[Executing Rule: {task_id}]\n"
            f"Instructions: {task.get('system_prompt_override', '')}\n"
            f"Context: Order {order['order_id']} for {customer_name} "
            f"({order['customer_phone']}) is currently '{order['status']}'.\n"
            f"IMPORTANT: You already have the customer's identity from the context above. "
            f"Do NOT ask for their phone number. Instead, greet them by name "
            f"(e.g. 'Hi {customer_name}, this is CustomerClaw support') and proceed "
            f"directly with the outreach message."
        )

        db.append_raw_message(sid, {
            "role": "system",
            "content": system_instruction,
        })
        db.set_session_status(sid, db.RUNNING)

        log.info("Enqueuing proactive session %s", sid)
        asyncio.create_task(run_agent(sid))


def reload_scheduler() -> None:
    """Hot-reload all APScheduler cron jobs from the scheduledTasks directory."""
    scheduler.remove_all_jobs()
    TASKS_DIR.mkdir(exist_ok=True)

    loaded = 0
    for file_path in TASKS_DIR.glob("*.json"):
        try:
            with open(file_path, "r") as f:
                task = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log.error("Corrupt task file %s — skipping: %s", file_path.name, e)
            continue
        except OSError as e:
            log.error("Cannot read task file %s: %s", file_path.name, e)
            continue

        if not task.get("enabled"):
            continue

        try:
            trigger = CronTrigger.from_crontab(task["cron"])
        except ValueError as e:
            log.error("Invalid cron expression in %s: %s", file_path.name, e)
            continue

        scheduler.add_job(
            execute_task,
            trigger=trigger,
            args=[task],
            id=task.get("task_id", file_path.stem),
            replace_existing=True,
        )
        loaded += 1

    log.info("Scheduler reloaded with %d active tasks.", loaded)


def start_poller() -> None:
    """Initialise and start the polling scheduler."""
    reload_scheduler()
    scheduler.start()
    log.info("CRM Poller started.")


def stop_poller() -> None:
    """Shut down the polling scheduler gracefully."""
    scheduler.shutdown()
