import asyncio
from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse
from agent.sse_events import (
    thought_queues, stream_queues,
    _ensure_thought_queue, _ensure_stream_queue,
)

router = APIRouter()


def _drain_queue(queue: asyncio.Queue) -> None:
    """Discard any items accumulated while no client was listening.

    This prevents stale events from a previous agent run being dumped
    at high speed when a new SSE connection is established (e.g. after
    the user navigates back to a session).
    """
    while not queue.empty():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            break


@router.get("/agent/thoughts/{session_id}")
async def thought_stream(request: Request, session_id: str):
    _ensure_thought_queue(session_id)
    queue = thought_queues[session_id]
    # Drain stale items accumulated while no client was connected
    _drain_queue(queue)

    async def generator():
        while True:
            if await request.is_disconnected():
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=15.0)
                # Dicts carry named-event metadata (e.g. {"event": "error", "data": html})
                if isinstance(item, dict):
                    yield item
                else:
                    yield {"data": item}
            except asyncio.TimeoutError:
                yield {"comment": "keepalive"}

    return EventSourceResponse(generator())


@router.get("/chat/stream/{session_id}")
async def chat_stream(request: Request, session_id: str):
    _ensure_stream_queue(session_id)
    queue = stream_queues[session_id]
    # Drain stale items accumulated while no client was connected
    _drain_queue(queue)

    async def generator():
        while True:
            if await request.is_disconnected():
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=15.0)
                if isinstance(item, dict):
                    yield item                      # {"event": "done", "data": html}
                else:
                    yield {"event": "chunk", "data": item}   # plain escaped token
            except asyncio.TimeoutError:
                yield {"comment": "keepalive"}

    return EventSourceResponse(generator())

