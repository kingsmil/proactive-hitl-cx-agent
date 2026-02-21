import asyncio
import json
import logging
from markupsafe import escape

import db
from agent.llm_client import call_llm, call_llm_streaming
from agent.tools import SAFE_TOOLS, HITL_TOOLS, TOOLS
from agent.sse_events import (
    thought_queues,
    stream_queues,
    emit_thought,
    emit_llm_thought,
    emit_error,
    emit_chat_append,
    emit_stream_done,
    emit_stream_error
)
from agent.whatsapp_client import send_whatsapp_message

log = logging.getLogger("agent")

# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------

_agent_locks = {}   # dict[str, asyncio.Lock] — one lock per session

def _ensure_stream_queue(session_id):
    if session_id not in stream_queues:
        stream_queues[session_id] = asyncio.Queue()

def _push_streamed_token_to_browser(session_id: str, token: str, loop: asyncio.AbstractEventLoop = None):
    """HTML-escape a token and push it into the SSE stream queue."""
    token_html = str(escape(token))
    if loop:
        loop.call_soon_threadsafe(
            stream_queues[session_id].put_nowait, token_html
        )
    else:
        stream_queues[session_id].put_nowait(token_html)

# ---------------------------------------------------------------------------
# Orchestrator loop
# ---------------------------------------------------------------------------

async def run_agent(session_id: str) -> None:
    # ── Guard: only one loop per session at a time ───────────────────────────
    if session_id not in _agent_locks:
        _agent_locks[session_id] = asyncio.Lock()
    lock = _agent_locks[session_id]
    if lock.locked():
        return  # another coroutine is already running this session — drop silently
    async with lock:
        await _run_agent_locked(session_id)


async def _run_agent_locked(session_id: str) -> None:
    if session_id not in thought_queues:
        thought_queues[session_id] = asyncio.Queue()

    _ensure_stream_queue(session_id)
    try:
        await _run_agent_body(session_id)
    except Exception as exc:
        await emit_error(session_id, str(exc))
        await emit_stream_error(session_id)
        db.set_session_status(session_id, "DONE")


async def _run_agent_body(session_id: str) -> None:
    # ── Phase A: resume from an approved HITL action ────────────────────────
    pending = db.get_pending_action(session_id)
    if pending:
        await emit_thought(
            session_id, "execute",
            "→ {} (approved by operator)".format(pending["tool_name"]),
        )
        result = HITL_TOOLS[pending["tool_name"]](**pending["arguments"])
        db.append_raw_message(session_id, {
            "role": "tool",
            "tool_call_id": pending["tool_call_id"],
            "content": result,
        })
        db.delete_pending_action(session_id)

    # ── Phase B: main loop ───────────────────────────────────────────────────
    while db.get_session(session_id)["status"] == "RUNNING":
        history = db.get_history(session_id)
        await emit_thought(session_id, "supervisor", "Evaluating conversation…")

        loop = asyncio.get_running_loop()

        def push_chunk(token: str):
            _push_streamed_token_to_browser(session_id, token, loop)

        response = await asyncio.to_thread(
            call_llm_streaming, history, TOOLS, push_chunk
        )

        choice = response["choices"][0]
        msg = choice["message"]
        finish_reason = choice["finish_reason"]

        # Emit expandable LLM call details to the Scrying Glass
        llm_preview = "LLM → {}".format(
            "tool_calls" if finish_reason == "tool_calls" else "reply"
        )
        await emit_llm_thought(session_id, llm_preview, history, response)

        if finish_reason == "tool_calls":
            # Store the full assistant message so tool results can reference it
            db.append_raw_message(session_id, {
                "role": "assistant",
                "content": msg.get("content"),
                "tool_calls": msg["tool_calls"],
            })

            for tc in msg["tool_calls"]:
                name = tc["function"]["name"]
                args_raw = tc["function"]["arguments"]
                # Sanitize common LLM artifacts that break pure JSON parsing
                if args_raw.rfind("}") != -1:
                    args_raw = args_raw[:args_raw.rfind("}") + 1]
                args = json.loads(args_raw)

                if name in SAFE_TOOLS:
                    await emit_thought(session_id, "execute", "→ {}".format(name))
                    result = SAFE_TOOLS[name](**args)
                    db.append_raw_message(session_id, {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    })

                elif name in HITL_TOOLS:
                    await emit_thought(
                        session_id, "hitl",
                        "⚠ {} requires operator approval".format(name),
                    )
                    db.save_pending_action(
                        session_id, name, args,
                        reasoning=msg.get("content") or "",
                        tool_call_id=tc["id"],
                    )
                    db.set_session_status(session_id, "PAUSED")
                    if name == "issue_refund":
                        ack = (
                            "We acknowledge your refund request for order {order_id} and your "
                            "reason for it (\"{reason}\"). We will escalate this to an agent "
                            "to help approve."
                        ).format(
                            order_id=args.get("order_id", ""),
                            reason=args.get("reason", ""),
                        )
                    else:
                        ack = (
                            "We acknowledge your request and your reason for it. "
                            "We will escalate this to an agent to help approve."
                        )
                    db.append_message(session_id, "assistant", ack)
                    # Push badge count to Seals tab via OOB swap
                    pending_count = len(db.get_all_paused_sessions())
                    oob_badge = (
                        '<span id="queue-count" hx-swap-oob="innerHTML">'
                        '{} awaiting</span>'
                    ).format(pending_count)
                    await emit_stream_done(session_id, ack, oob_html=oob_badge)
                    
                    # Outbound WhatsApp intercept for HITL acknowledgment
                    sess = db.get_session(session_id)
                    if sess.get("channel") == "whatsapp":
                        # The session ID is the phone number for WhatsApp
                        await send_whatsapp_message(session_id, ack)
                        
                    return  # halt — resumed via /actions/approve

        else:  # finish_reason == "stop"
            await emit_thought(session_id, "reason", "Composing reply…")
            db.append_message(session_id, "assistant", msg["content"])
            await emit_stream_done(session_id, msg["content"])
            db.set_session_status(session_id, "DONE")
            
            # Outbound WhatsApp intercept for standard text reply
            sess = db.get_session(session_id)
            if sess.get("channel") == "whatsapp":
                # The session ID is the phone number for WhatsApp
                await send_whatsapp_message(session_id, msg["content"])
                
            return
