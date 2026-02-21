import asyncio
import json
import logging
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import db
from agent import run_agent

log = logging.getLogger("poller")

scheduler = AsyncIOScheduler()
TASKS_DIR = Path("scheduledTasks")


async def execute_task(task: dict) -> None:
    """Fires when the cron matches. Uses db public API to find orders and queue sessions."""
    if not task.get("enabled", False):
        return

    # B1/B2: use the db public helper — SQL construction stays in the data layer
    stale_orders = db.query_orders_by_filters(task.get("filters", {}))

    if not stale_orders:
        return

    log.info(
        "Task '%s' found %d applicable orders.",
        task.get("task_id"), len(stale_orders)
    )

    for order in stale_orders:
        # Mark before dispatching to prevent duplicate triggers on re-entry
        db.mark_order_outreached(order["order_id"])

        sid = "proactive-{task_id}-{order_id}".format(
            task_id=task.get("task_id"),
            order_id=order["order_id"],
        )
        db.get_or_create_session(sid, channel="proactive")

        # N5: single template string — easy to test/modify
        synthetic_msg = (
            "[System Executing Rule: {task_id}]\n"
            "Instructions: {instructions}\n"
            "Context: Order {order_id} for {phone} is currently '{status}'."
        ).format(
            task_id=task.get("task_id", "unknown"),
            instructions=task.get("system_prompt_override", ""),
            order_id=order["order_id"],
            phone=order["customer_phone"],
            status=order["status"],
        )

        db.append_message(sid, "user", synthetic_msg)
        db.set_session_status(sid, "RUNNING")

        log.info("Enqueuing proactive session %s", sid)
        asyncio.create_task(run_agent(sid))


def reload_scheduler() -> None:
    """Hot-reloads all APScheduler cron jobs from the scheduledTasks directory."""
    scheduler.remove_all_jobs()

    TASKS_DIR.mkdir(exist_ok=True)

    loaded = 0
    for file_path in TASKS_DIR.glob("*.json"):
        # N3: narrow exception handling — distinguish parse errors from runtime errors
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
    """Initialises and starts the polling runtime."""
    reload_scheduler()
    scheduler.start()
    log.info("CRM Poller started.")


def stop_poller() -> None:
    scheduler.shutdown()
