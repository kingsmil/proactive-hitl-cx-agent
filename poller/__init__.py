import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import db
from agent import run_agent

log = logging.getLogger("poller")

scheduler = AsyncIOScheduler()
TASKS_DIR = Path("scheduledTasks")

def _build_sql_query(filters: dict) -> tuple[str, list]:
    """Dynamically build safe SQLite query from the JSON filters."""
    conditions = ["status = ?", "outreached = 0"]
    params = [filters.get("status", "delayed")]
    
    # Handle time delay filter
    if "min_hours_since_update" in filters:
        hours = filters["min_hours_since_update"]
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        conditions.append("last_updated < ?")
        params.append(cutoff)
        
    # Handle VIP phone prefix
    if "phone_prefix" in filters:
        conditions.append("customer_phone LIKE ?")
        params.append(f"{filters['phone_prefix']}%")
        
    where_clause = " AND ".join(conditions)
    query = f"SELECT * FROM orders WHERE {where_clause}"
    return query, params


async def execute_task(task: dict):
    """Fires when the cron matches. Finds orders and queues sessions."""
    if not task.get("enabled", False):
        return

    query, params = _build_sql_query(task.get("filters", {}))
    
    conn = db._conn()
    rows = conn.execute(query, params).fetchall()
    stale_orders = [dict(r) for r in rows]

    if not stale_orders:
        return

    log.info(f"Task '{task.get('task_id')}' found {len(stale_orders)} applicable orders.")

    for order in stale_orders:
        # Prevent re-triggering
        db.mark_order_outreached(order["order_id"])
        
        sid = f"proactive-{task.get('task_id')}-{order['order_id']}"
        db.get_or_create_session(sid, channel="proactive")
        
        synthetic_msg = f"[System Executing Rule: {task.get('task_id')}]\n"
        synthetic_msg += f"Instructions: {task.get('system_prompt_override')}\n"
        synthetic_msg += f"Context: Order {order['order_id']} for {order['customer_phone']} is currently '{order['status']}'."
        
        db.append_message(sid, "user", synthetic_msg)
        db.set_session_status(sid, "RUNNING")
        
        log.info(f"Enqueuing proactive session {sid}")
        # Dispatch to orchestrator in the background
        asyncio.create_task(run_agent(sid))


def reload_scheduler():
    """Reads JSON files from scheduledTasks and repopulates the APScheduler."""
    scheduler.remove_all_jobs()
    
    if not TASKS_DIR.exists():
        TASKS_DIR.mkdir(exist_ok=True)
        
    loaded = 0
    for file_path in TASKS_DIR.glob("*.json"):
        try:
            with open(file_path, "r") as f:
                task = json.load(f)
                
            if task.get("enabled"):
                trigger = CronTrigger.from_crontab(task["cron"])
                scheduler.add_job(
                    execute_task, 
                    trigger=trigger, 
                    args=[task], 
                    id=task.get("task_id", file_path.stem),
                    replace_existing=True
                )
                loaded += 1
        except Exception as e:
            log.error(f"Failed to load task {file_path.name}: {e}")
            
    log.info(f"Scheduler reloaded with {loaded} active tasks.")


def start_poller():
    """Initializes and starts the polling runtime."""
    reload_scheduler()
    scheduler.start()
    log.info("CRM Poller started.")


def stop_poller():
    scheduler.shutdown()
