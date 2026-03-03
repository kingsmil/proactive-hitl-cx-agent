import asyncio
import json
import logging
from markupsafe import escape

import db
from agent.llm_client import (
    call_llm, call_llm_streaming,
    PROACTIVE_SYSTEM_PROMPT,
)
from agent.tools import SAFE_TOOLS, HITL_TOOLS, TOOLS, get_ack_message, sanitize_json_fragment, validate_refund
from agent.sse_events import (
    thought_queues,
    stream_queues,
    _ensure_thought_queue,
    _ensure_stream_queue,
    emit_thought,
    emit_event_log_entry,
    emit_llm_thought,
    emit_error,
    emit_chat_append,
    emit_stream_done,
    emit_stream_error,
)
from agent.telegram_client import send_telegram_message

log = logging.getLogger("agent")

# ---------------------------------------------------------------------------
# Locks — one asyncio.Lock per session
# ---------------------------------------------------------------------------

_agent_locks: dict[str, asyncio.Lock] = {}


def _push_streamed_token_to_browser(
    session_id: str,
    token: str,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """HTML-escape a token and push it into the SSE stream queue."""
    token_html = str(escape(token))
    if loop:
        loop.call_soon_threadsafe(stream_queues[session_id].put_nowait, token_html)
    else:
        stream_queues[session_id].put_nowait(token_html)


async def _dispatch_reply(session_id: str, content: str) -> None:
    """Deliver a completed agent reply to non-SSE channel sinks (e.g. Telegram)."""
    sess = db.get_session(session_id)
    if sess and sess.get("channel") == "telegram":
        await send_telegram_message(session_id, content)


# ---------------------------------------------------------------------------
# Orchestrator entry-point
# ---------------------------------------------------------------------------

async def run_agent(session_id: str) -> None:
    """Start (or re-enter) the agent loop for a session, guarded by a per-session lock."""
    if session_id not in _agent_locks:
        _agent_locks[session_id] = asyncio.Lock()
    lock = _agent_locks[session_id]
    if lock.locked():
        return  # another coroutine is already running — drop silently
    async with lock:
        await _run_agent_locked(session_id)


async def _run_agent_locked(session_id: str) -> None:
    _ensure_thought_queue(session_id)
    _ensure_stream_queue(session_id)
    try:
        await _run_agent_body(session_id)
    except Exception as exc:
        log.error("Agent loop crashed for %s: %s", session_id, exc, exc_info=True)
        await emit_error(session_id, str(exc))
        await emit_stream_error(session_id)
        db.set_session_status(session_id, db.DONE)


async def _run_agent_body(session_id: str) -> None:
    # Detect proactive sessions and swap the system prompt so the agent
    # greets by name instead of asking for a phone number.
    session = db.get_session(session_id)
    is_proactive = (session or {}).get("channel") == "proactive"
    _system_prompt = PROACTIVE_SYSTEM_PROMPT if is_proactive else None

    # The streaming placeholder (#reply-body-{session_id}) only exists when a
    # web user just submitted a message — the HTTP response renders chat_pane.html
    # which creates it.  It does NOT exist when:
    #   • The session is from an external channel (Telegram, proactive)
    #   • We're re-entering after HITL (approve or reject) — the ack already
    #     consumed the placeholder via emit_stream_done
    # We detect these by checking that channel is web AND the last message in
    # history is from "user" (i.e. freshly submitted, not a tool-rejection msg).
    channel = (session or {}).get("channel", "web")
    history_peek = db.get_history(session_id)
    last_role = history_peek[-1].get("role", "") if history_peek else ""
    _has_streaming_placeholder = channel == "web" and last_role == "user"

    # ── Phase A: resume from an approved HITL action ──────────────────────────
    #
    # `from_hitl` remembers that the streaming bubble was already consumed by the
    # HITL ack message.  After approval the DOM has no #reply-body-{session_id},
    # so the final reply must use emit_chat_append (full bubble) rather than
    # emit_stream_done (which targets the non-existent placeholder).
    from_hitl = False
    pending = db.get_pending_action(session_id)
    if pending:
        from_hitl = True
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
        await emit_event_log_entry(session_id, "tool_result", str(result)[:200], actor="system")
        db.delete_pending_action(session_id)

    # ── Phase B: main LLM loop ────────────────────────────────────────────────
    #
    # `_suppress_stream` controls whether token chunks are pushed to the browser:
    #   • True  when from_hitl (no streaming bubble in DOM)
    #   • True  during intermediate tool-call passes (garbled tokens would appear)
    #   • False for the final stop-reply pass (chunks appear live in the bubble)
    #
    # We use a default-arg trick in push_chunk so the closure captures a *copy*
    # of the flag value at the time the function is defined each iteration,
    # rather than binding to the outer variable by reference.
    _suppress_stream = True

    while db.get_session(session_id)["status"] == db.RUNNING:
        history = db.get_history(session_id)
        await emit_thought(session_id, "supervisor", "Evaluating conversation…")

        loop = asyncio.get_running_loop()

        def push_chunk(token: str, _go: bool = not _suppress_stream) -> None:
            if _go:
                _push_streamed_token_to_browser(session_id, token, loop)

        response = await asyncio.to_thread(call_llm_streaming, history, TOOLS, push_chunk, _system_prompt)

        choice = response["choices"][0]
        msg = choice["message"]
        finish_reason = choice["finish_reason"]

        llm_preview = "LLM → {}".format(
            "tool_calls" if finish_reason == "tool_calls" else "reply"
        )
        await emit_llm_thought(session_id, llm_preview, history, response)

        if finish_reason == "tool_calls":
            db.append_raw_message(session_id, {
                "role": "assistant",
                "content": msg.get("content"),
                "tool_calls": msg["tool_calls"],
            })

            # Suppress streaming on this intermediate pass; re-enable afterwards
            # so the NEXT pass (the final stop-reply) streams tokens live.
            _suppress_stream = True

            for tc in msg["tool_calls"]:
                name = tc["function"]["name"]
                args_raw = sanitize_json_fragment(tc["function"]["arguments"])
                args = json.loads(args_raw)

                if name in SAFE_TOOLS:
                    await emit_thought(session_id, "execute", "→ {}".format(name))
                    result = SAFE_TOOLS[name](**args)
                    db.append_raw_message(session_id, {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    })
                    await emit_event_log_entry(session_id, "tool_result", str(result)[:200], actor="system")

                elif name in HITL_TOOLS:
                    # Pre-flight validation for refunds
                    if name == "issue_refund":
                        refund_error = validate_refund(
                            args.get("order_id", ""),
                            args.get("customer_phone", ""),
                        )
                        if refund_error:
                            await emit_thought(
                                session_id, "execute",
                                "✗ {} rejected: {}".format(name, refund_error),
                            )
                            db.append_raw_message(session_id, {
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": "Cannot issue refund: {}".format(refund_error),
                            })
                            continue  # skip HITL gate, let LLM loop handle the error

                    await emit_thought(
                        session_id, "hitl",
                        "⚠ {} requires operator approval".format(name),
                    )
                    db.save_pending_action(
                        session_id, name, args,
                        reasoning=msg.get("content") or "",
                        tool_call_id=tc["id"],
                    )
                    if name == "issue_refund" and "order_id" in args:
                        db.log_order_event(
                            args["order_id"],
                            "refund_requested",
                            "Refund of ${:.2f} requested — awaiting approval".format(
                                args.get("amount", 0)
                            ),
                            actor="agent",
                            session_id=session_id,
                        )
                    db.set_session_status(session_id, db.PAUSED)
                    ack = get_ack_message(name, args)
                    db.append_message(session_id, "assistant", ack)
                    await emit_event_log_entry(session_id, "agent_reply", ack)
                    if _has_streaming_placeholder:
                        # Web chat: streaming bubble is in the DOM, replace it.
                        await emit_stream_done(session_id, ack)
                    else:
                        # External channel (Telegram, etc.): no placeholder exists.
                        await emit_chat_append(session_id, ack)
                    await _dispatch_reply(session_id, ack)
                    return  # halt — agent re-entered via /actions/approve


        else:  # finish_reason == "stop"
            await emit_thought(session_id, "reason", "Composing reply…")
            db.append_message(session_id, "assistant", msg["content"])
            await emit_event_log_entry(session_id, "agent_reply", msg["content"])
            if from_hitl or not _has_streaming_placeholder:
                # Post-HITL: streaming bubble was already replaced by the ack.
                # External channel: no streaming placeholder exists in the DOM.
                # In both cases, append a fresh complete bubble instead.
                await emit_chat_append(session_id, msg["content"])
            else:
                # Normal web chat flow: replace the streaming placeholder.
                await emit_stream_done(session_id, msg["content"])
            await _dispatch_reply(session_id, msg["content"])
            db.set_session_status(session_id, db.DONE)
            return
