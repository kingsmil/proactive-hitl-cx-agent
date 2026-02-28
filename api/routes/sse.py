import asyncio
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from agent.sse_events import (
    thought_queues, stream_queues,
    _ensure_thought_queue, _ensure_stream_queue,
)

router = APIRouter()


def _drain_queue(queue: asyncio.Queue) -> None:
    """Discard stale items accumulated while no client was listening.

    This prevents old events from a previous agent run being dumped
    at high speed when a new SSE connection is established (e.g. after
    the user navigates back to a session).
    """
    while not queue.empty():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            break


@router.get("/agent/thoughts/{session_id}")
async def thought_stream(
    request: Request, session_id: str,
) -> EventSourceResponse:
    """SSE stream of agent thought-trace entries for a session."""
    _ensure_thought_queue(session_id)
    queue = thought_queues[session_id]
    _drain_queue(queue)

    async def generator() -> AsyncGenerator:
        while True:
            if await request.is_disconnected():
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=15.0)
                if isinstance(item, dict):
                    yield item
                else:
                    yield {"data": item}
            except asyncio.TimeoutError:
                yield {"comment": "keepalive"}

    return EventSourceResponse(generator())


@router.get("/chat/stream/{session_id}")
async def chat_stream(
    request: Request, session_id: str,
) -> EventSourceResponse:
    """SSE stream of chat tokens and final replies for a session."""
    _ensure_stream_queue(session_id)
    queue = stream_queues[session_id]
    _drain_queue(queue)

    async def generator() -> AsyncGenerator:
        while True:
            if await request.is_disconnected():
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=15.0)
                if isinstance(item, dict):
                    yield item
                else:
                    yield {"event": "chunk", "data": item}
            except asyncio.TimeoutError:
                yield {"comment": "keepalive"}

    return EventSourceResponse(generator())
