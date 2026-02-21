import asyncio
from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse
from agent.sse_events import thought_queues, stream_queues

router = APIRouter()

@router.get("/agent/thoughts/{session_id}")
async def thought_stream(request: Request, session_id: str):
    if session_id not in thought_queues:
        thought_queues[session_id] = asyncio.Queue()
    queue = thought_queues[session_id]

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
    if session_id not in stream_queues:
        stream_queues[session_id] = asyncio.Queue()
    queue = stream_queues[session_id]

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
