import asyncio

# Per-session SSE queues — read by api/app.py's SSE endpoint,
# written by emit_thought() (implemented in Phase 3).
thought_queues = {}  # type: dict[str, asyncio.Queue]


# Phase 3 will replace this stub with the full orchestrator loop.
async def run_agent(session_id: str) -> None:
    pass
