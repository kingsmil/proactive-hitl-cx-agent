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
    _ensure_stream_queue,
    emit_thought,
    emit_llm_thought,
    emit_error,
    emit_chat_append,
    emit_stream_done,
    emit_stream_error
)
from agent.telegram_client import send_telegram_message

log = logging.getLogger("agent")

# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------

_agent_locks = {}   # dict[str, asyncio.Lock] — one lock per session

def _push_streamed_token_to_browser(session_id: str, token: str, loop: asyncio.AbstractEventLoop = None):
    """HTML-escape a token and push it into the SSE stream queue."""
    token_html = str(escape(token))
    if loop:
        loop.call_soon_threadsafe(
            stream_queues[session_id].put_nowait, token_html
        )
    else:
        stream_queues[session_id].put_nowait(token_html)


async def _dispatch_reply(session_id: str, content: str) -> None:
    """Deliver a completed agent reply to any non-SSE channel sinks (e.g. Telegram)."""
    sess = db.get_session(session_id)
    if sess and sess.get("channel") == "telegram":
        await send_telegram_message(session_id, content)

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


from api.routes.config import templates
from agent.tools import TOOL_ACK_MESSAGES

def _sanitize_json_fragment(args_raw: str) -> str:
    """Sanitize common LLM artifacts that break pure JSON parsing."""
    if args_raw.rfind("}") != -1:
        return args_raw[:args_raw.rfind("}") + 1]
    return args_raw

async def _run_agent_locked(session_id: str) -> None:
    _ensure_stream_queue(session_id)
    try:
        await _run_agent_body(session_id)
    except Exception as exc:
        log.error("Agent loop crashed for session %s: %s", session_id, exc, exc_info=True)
        await emit_error(session_id, str(exc))
        await emit_stream_error(session_id)
        db.set_session_status(session_id, "DONE")

async def _resume_hitl_action_if_pending(session_id: str) -> None:
    """Execute a HITL action if one was approved by an operator."""
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

async def _handle_final_reply(session_id: str, msg: dict) -> None:
    """Store the LLM's final reply in DB and emit to SSE clients."""
    await emit_thought(session_id, "reason", "Composing reply…")
    db.append_message(session_id, "assistant", msg["content"])
    await emit_stream_done(session_id, msg["content"])
    await _dispatch_reply(session_id, msg["content"])
    db.set_session_status(session_id, "DONE")

async def _escalate_hitl_tool(session_id: str, name: str, args: dict, msg_content: str, tc_id: str) -> None:
    """Pause the session to require operator approval to proceed with a HITL tool."""
    await emit_thought(
        session_id, "hitl",
        "⚠ {} requires operator approval".format(name),
    )
    db.save_pending_action(
        session_id, name, args,
        reasoning=msg_content or "",
        tool_call_id=tc_id,
    )
    db.set_session_status(session_id, "PAUSED")
    
    ack = TOOL_ACK_MESSAGES.get(name, "We acknowledge your request. We will escalate this to an agent to help approve.")
    try:
        ack_formatted = ack.format(**args)
    except KeyError:
        ack_formatted = ack

    db.append_message(session_id, "assistant", ack_formatted)
    
    pending_count = len(db.get_all_paused_sessions())
    oob_badge = templates.get_template("partials/queue_badge.html").render(pending_count=pending_count)

    await emit_stream_done(session_id, ack_formatted, oob_html=oob_badge)
    await _dispatch_reply(session_id, ack_formatted)

async def _execute_safe_tool(session_id: str, name: str, args: dict, tc_id: str) -> None:
    """Execute a safe tool that does not require operator approval."""
    await emit_thought(session_id, "execute", "→ {}".format(name))
    result = SAFE_TOOLS[name](**args)
    db.append_raw_message(session_id, {
        "role": "tool",
        "tool_call_id": tc_id,
        "content": result,
    })

async def _run_agent_body(session_id: str) -> None:
    # ── Phase A: resume from an approved HITL action ────────────────────────
    await _resume_hitl_action_if_pending(session_id)

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
                args_raw = _sanitize_json_fragment(tc["function"]["arguments"])
                args = json.loads(args_raw)

                if name in SAFE_TOOLS:
                    await _execute_safe_tool(session_id, name, args, tc["id"])

                elif name in HITL_TOOLS:
                    await _escalate_hitl_tool(session_id, name, args, msg.get("content"), tc["id"])
                    return  # halt — resumed via /actions/approve
        else:  # finish_reason == "stop"
            await _handle_final_reply(session_id, msg)
            return
