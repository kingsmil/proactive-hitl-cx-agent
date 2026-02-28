import asyncio
import json
from pathlib import Path
from markupsafe import escape
from jinja2 import Environment, FileSystemLoader
from agent.tools import sanitize_json_fragment
import db

# ---------------------------------------------------------------------------
# SSE thought queues — per-session asyncio.Queue, read by api/app.py
# ---------------------------------------------------------------------------

thought_queues = {}   # type: dict[str, asyncio.Queue]
stream_queues  = {}   # type: dict[str, asyncio.Queue]  — token stream per session


def _ensure_thought_queue(session_id):
    if session_id not in thought_queues:
        thought_queues[session_id] = asyncio.Queue()


def _ensure_stream_queue(session_id):
    if session_id not in stream_queues:
        stream_queues[session_id] = asyncio.Queue()

# Jinja2 env for rendering thought_entry.html without a FastAPI Request object
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "frontend" / "templates"
_jinja = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)))


# ---------------------------------------------------------------------------
# Thought streaming
# ---------------------------------------------------------------------------

async def emit_thought(session_id: str, node: str, preview: str) -> None:
    """Render a thought_entry partial, push it into the SSE queue, and persist to DB."""
    db.log_session_thought(session_id, node, preview)
    html = _jinja.get_template("partials/thought_entry.html").render(
        session_id=session_id, node=node, preview=preview
    )
    _ensure_thought_queue(session_id)
    await thought_queues[session_id].put(html)


async def emit_llm_thought(
    session_id: str,
    preview: str,
    request_messages: list,
    response: dict,
) -> None:
    """Render an expandable LLM call thought entry and persist summary to DB."""
    # Compact request summary — roles + truncated content
    req_lines = []
    for m in request_messages:
        role = m.get("role", "?")
        text = (m.get("content") or "")[:200]
        if len(m.get("content") or "") > 200:
            text += "…"
        req_lines.append("{}: {}".format(role, escape(text)))
    req_summary = "\n".join(req_lines)

    # Compact response summary
    try:
        choice = response["choices"][0]
        msg = choice.get("message", {})
        resp_parts = []
        if msg.get("content"):
            resp_parts.append(str(escape(msg["content"][:500])))
            if len(msg["content"]) > 500:
                resp_parts[-1] += "…"
        for tc in msg.get("tool_calls", []):
            args_str = sanitize_json_fragment(tc["function"]["arguments"])
            resp_parts.append("tool_call: {}({})".format(
                escape(tc["function"]["name"]),
                args_str[:200],
            ))
        resp_summary = "\n".join(resp_parts) if resp_parts else "(empty)"
    except (KeyError, IndexError):
        resp_summary = json.dumps(response, indent=2)[:500]

    # Persist with details so the expandable view works on reload too
    details = json.dumps({
        "request_summary": req_summary,
        "request_count": len(request_messages),
        "response_summary": resp_summary,
    })
    db.log_session_thought(session_id, "llm", preview, details_json=details)

    html = _jinja.get_template("partials/thought_llm_entry.html").render(
        session_id=session_id,
        preview=preview,
        request_messages=req_summary,
        request_count=len(request_messages),
        response_text=resp_summary,
    )
    _ensure_thought_queue(session_id)
    await thought_queues[session_id].put(html)


async def emit_error(session_id: str, message: str) -> None:
    """Render an error toast partial and push it as a named 'error' SSE event."""
    html = _jinja.get_template("partials/error_toast.html").render(message=message)
    _ensure_thought_queue(session_id)
    await thought_queues[session_id].put({"event": "error", "data": html})


async def emit_chat_append(session_id: str, reply: str) -> None:
    """Push a complete message bubble as an 'append' event to stream_queues."""
    html = _jinja.get_template("partials/agent_chat_append.html").render(reply=reply)
    _ensure_stream_queue(session_id)
    await stream_queues[session_id].put({"event": "append", "data": html})


async def emit_user_message(session_id: str, content: str) -> None:
    """Push an incoming user message bubble as an 'append' event to stream_queues.

    Used when messages arrive from external channels (e.g. Telegram) so the
    operator's chat pane updates in real-time without needing a page refresh.
    """
    html = _jinja.get_template("partials/message_bubble.html").render(
        role="user", content=content, is_manual=False
    )
    _ensure_stream_queue(session_id)
    await stream_queues[session_id].put({"event": "append", "data": html})


async def emit_stream_done(session_id: str, reply: str, oob_html: str = "") -> None:
    """Push the final agent bubble HTML as a named 'done' event to stream_queues."""
    html = _jinja.get_template("partials/agent_stream_done.html").render(reply=reply, oob_html=oob_html)
    _ensure_stream_queue(session_id)
    await stream_queues[session_id].put({"event": "done", "data": html})


async def emit_stream_error(session_id: str) -> None:
    """Push an error-state bubble as 'done' so the pending bubble doesn't hang."""
    html = _jinja.get_template("partials/agent_stream_error.html").render()
    _ensure_stream_queue(session_id)
    await stream_queues[session_id].put({"event": "done", "data": html})
