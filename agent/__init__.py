import asyncio
import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request

from markupsafe import escape

log = logging.getLogger("agent")

import certifi

_ssl_ctx = ssl.create_default_context(cafile=certifi.where())

from jinja2 import Environment, FileSystemLoader

import db

# ---------------------------------------------------------------------------
# SSE thought queues — per-session asyncio.Queue, read by api/app.py
# ---------------------------------------------------------------------------

thought_queues = {}   # type: dict[str, asyncio.Queue]
stream_queues  = {}   # type: dict[str, asyncio.Queue]  — token stream per session
_agent_locks   = {}   # type: dict[str, asyncio.Lock]   — one lock per session


def _ensure_stream_queue(session_id):
    if session_id not in stream_queues:
        stream_queues[session_id] = asyncio.Queue()

# Jinja2 env for rendering thought_entry.html without a FastAPI Request object
_jinja = Environment(loader=FileSystemLoader("frontend/templates"))

# ---------------------------------------------------------------------------
# LLM integration — direct HTTP to OpenRouter or Gemini OpenAI-compat layer
# ---------------------------------------------------------------------------

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_URL     = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

DEFAULT_MODEL        = "openai/gpt-oss-120b"
GEMINI_DEFAULT_MODEL = "gemini-2.0-flash"

SYSTEM_PROMPT = """You are CustomerClaw, a concise customer-support assistant for an e-commerce platform.

Guidelines:
- Always call check_order_status before making any decisions about an order.
- Call issue_refund only when the customer is clearly owed a refund and you have confirmed the order status.
- Be brief and empathetic. One short paragraph per reply.
- Never invent order details — only use what the tools return."""


# ---------------------------------------------------------------------------
# LLM helpers — shared config, request building, streaming
# ---------------------------------------------------------------------------

def _build_llm_endpoint_config():
    """Resolve which LLM provider to use and return (url, model, headers).

    Checks IS_GEMINI_MODEL env-var to pick Gemini vs OpenRouter.
    """
    is_gemini = bool(os.environ.get("IS_GEMINI_MODEL", ""))

    if is_gemini:
        url     = GEMINI_URL
        model   = os.environ.get("GEMINI_MODEL", GEMINI_DEFAULT_MODEL)
        api_key = os.environ.get("GEMINI_API_KEY", "")
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer {}".format(api_key),
        }
    else:
        url     = OPENROUTER_URL
        model   = db.get_setting("model", DEFAULT_MODEL)
        api_key = db.get_setting("openrouter_api_key",
                                 os.environ.get("OPENROUTER_API_KEY", ""))
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer {}".format(api_key),
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": "CustomerClaw",
        }

    return url, model, headers


def _build_llm_request_payload(model, history, tools, *, stream=False):
    """Build the JSON request body for the LLM API call.

    Returns (body_dict, encoded_payload).
    """
    body_dict = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + history,
        "tools": tools,
        "tool_choice": "auto",
    }
    if stream:
        body_dict["stream"] = True
    payload = json.dumps(body_dict).encode("utf-8")
    return body_dict, payload


def _push_streamed_token_to_browser(session_id, token, _loop=None):
    """HTML-escape a token and push it into the SSE stream queue.

    Uses call_soon_threadsafe when running from a background thread (_loop is set),
    otherwise pushes directly via put_nowait.
    """
    token_html = str(escape(token))
    if _loop:
        _loop.call_soon_threadsafe(
            stream_queues[session_id].put_nowait, token_html
        )
    else:
        stream_queues[session_id].put_nowait(token_html)


def _accumulate_tool_call_argument_deltas(tool_calls_by_index, delta_tool_calls):
    """Merge incremental tool-call chunks into a consolidated dict keyed by index.

    Each streaming chunk may carry partial function name / argument fragments.
    This accumulates them so the final result contains complete tool calls.
    """
    for i, tc in enumerate(delta_tool_calls):
        idx = tc.get("index", i)  # Gemini omits "index"; fall back to position
        if idx not in tool_calls_by_index:
            tool_calls_by_index[idx] = {
                "id": "", "type": "function",
                "function": {"name": "", "arguments": ""},
            }
        if tc.get("id"):
            tool_calls_by_index[idx]["id"] = tc["id"]
        fn = tc.get("function", {})
        if fn.get("name"):
            tool_calls_by_index[idx]["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            tool_calls_by_index[idx]["function"]["arguments"] += fn["arguments"]


def _assemble_streamed_response_into_message(text_content, tool_calls_by_index, finish_reason):
    """Convert accumulated streaming state into a response dict matching call_llm's format.

    Returns {"choices": [{"finish_reason": ..., "message": {...}}]}.
    """
    msg = {"role": "assistant", "content": text_content or None}
    if tool_calls_by_index:
        msg["tool_calls"] = [
            tool_calls_by_index[i] for i in sorted(tool_calls_by_index)
        ]
    return {"choices": [{"finish_reason": finish_reason, "message": msg}]}


def _execute_llm_request_with_retry(url, headers, payload):
    """Send a non-streaming POST and return the parsed JSON response.

    Retries up to 3 times with exponential backoff on 429 rate limits.
    """
    for attempt in range(3):
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, context=_ssl_ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                log.debug("LLM 429 — retry %d/2 in %ds", attempt + 1, 2 ** attempt + 1)
                time.sleep(2 ** attempt + 1)
                continue
            body = e.read().decode("utf-8")
            raise RuntimeError("OpenRouter {} {}: {}".format(e.code, e.reason, body))


# ---------------------------------------------------------------------------
# Public LLM call functions
# ---------------------------------------------------------------------------

def call_llm(history, tools):
    """POST to OpenRouter or Gemini and return the parsed response dict.

    Set IS_GEMINI_MODEL=1 in the environment to route through Gemini.
    """
    url, model, headers = _build_llm_endpoint_config()
    body_dict, payload = _build_llm_request_payload(model, history, tools)
    log.debug("LLM REQUEST  → %s\n%s", url, json.dumps(body_dict, indent=2))
    response = _execute_llm_request_with_retry(url, headers, payload)
    log.debug("LLM RESPONSE ←\n%s", json.dumps(response, indent=2))
    return response


def call_llm_streaming(session_id, history, tools, _loop=None):
    """POST with stream=True, pushing each token to the browser via SSE.

    Returns an assembled response dict in the same shape as call_llm.
    """
    url, model, headers = _build_llm_endpoint_config()
    body_dict, payload = _build_llm_request_payload(model, history, tools, stream=True)
    log.debug("LLM REQUEST (stream) → %s\n%s", url, json.dumps(body_dict, indent=2))

    _ensure_stream_queue(session_id)

    for attempt in range(3):
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            text_content, tool_calls_by_index, finish_reason = \
                _read_streaming_response_chunks(session_id, req, _loop)

            assembled = _assemble_streamed_response_into_message(
                text_content, tool_calls_by_index, finish_reason
            )
            log.debug("LLM RESPONSE (assembled) ←\n%s", json.dumps(assembled, indent=2))
            return assembled

        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                log.debug("LLM 429 — retry %d/2 in %ds", attempt + 1, 2 ** attempt + 1)
                time.sleep(2 ** attempt + 1)
                continue
            body = e.read().decode("utf-8")
            raise RuntimeError("OpenRouter {} {}: {}".format(e.code, e.reason, body))


def _read_streaming_response_chunks(session_id, req, _loop):
    """Open the HTTP stream and process each SSE line as it arrives.

    Returns (text_content, tool_calls_by_index, finish_reason).
    """
    text_content = ""
    tool_calls_by_index = {}
    finish_reason = None

    with urllib.request.urlopen(req, context=_ssl_ctx) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8").rstrip("\r\n")
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue

            choice = chunk.get("choices", [{}])[0]
            delta  = choice.get("delta", {})
            fr     = choice.get("finish_reason")
            if fr:
                finish_reason = fr

            # Stream text token to browser in real time
            token = delta.get("content") or ""
            if token:
                text_content += token
                _push_streamed_token_to_browser(session_id, token, _loop)

            # Accumulate tool-call fragments
            if delta.get("tool_calls"):
                _accumulate_tool_call_argument_deltas(
                    tool_calls_by_index, delta["tool_calls"]
                )

    return text_content, tool_calls_by_index, finish_reason


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def check_order_status(order_id: str) -> str:
    """Query the orders table and return a human-readable status string."""
    row = db._conn().execute(
        "SELECT status, last_updated FROM orders WHERE order_id = ?", (order_id,)
    ).fetchone()
    if row is None:
        return "Order {} not found.".format(order_id)
    return "Order {}: status='{}', last updated {}.".format(
        order_id, row["status"], row["last_updated"]
    )


def issue_refund(order_id: str, amount: float, reason: str) -> str:
    """Stub — triggers the HITL gate; only executed after operator approval."""
    return "Refund of ${:.2f} issued for order {} — reason: {}.".format(
        amount, order_id, reason
    )


SAFE_TOOLS = {"check_order_status": check_order_status}
HITL_TOOLS = {"issue_refund": issue_refund}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_order_status",
            "description": "Look up the current status of a customer order.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "The order identifier, e.g. ORD-001",
                    }
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "issue_refund",
            "description": "Issue a full or partial refund to a customer. Requires operator approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "amount": {
                        "type": "number",
                        "description": "Refund amount in USD",
                    },
                    "reason": {"type": "string"},
                },
                "required": ["order_id", "amount", "reason"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Thought streaming
# ---------------------------------------------------------------------------

async def emit_thought(session_id: str, node: str, preview: str) -> None:
    """Render a thought_entry partial and push it into the SSE queue."""
    html = _jinja.get_template("partials/thought_entry.html").render(
        session_id=session_id, node=node, preview=preview
    )
    if session_id not in thought_queues:
        thought_queues[session_id] = asyncio.Queue()
    await thought_queues[session_id].put(html)


async def emit_llm_thought(
    session_id: str,
    preview: str,
    request_messages: list,
    response: dict,
) -> None:
    """Render an expandable LLM call thought entry."""
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
            resp_parts.append("tool_call: {}({})".format(
                escape(tc["function"]["name"]),
                tc["function"]["arguments"][:200],
            ))
        resp_summary = "\n".join(resp_parts) if resp_parts else "(empty)"
    except (KeyError, IndexError):
        resp_summary = json.dumps(response, indent=2)[:500]

    html = _jinja.get_template("partials/thought_llm_entry.html").render(
        session_id=session_id,
        preview=preview,
        request_messages=req_summary,
        request_count=len(request_messages),
        response_text=resp_summary,
    )
    if session_id not in thought_queues:
        thought_queues[session_id] = asyncio.Queue()
    await thought_queues[session_id].put(html)


async def emit_error(session_id: str, message: str) -> None:
    """Render an error toast partial and push it as a named 'error' SSE event."""
    html = _jinja.get_template("partials/error_toast.html").render(message=message)
    if session_id not in thought_queues:
        thought_queues[session_id] = asyncio.Queue()
    await thought_queues[session_id].put({"event": "error", "data": html})


async def emit_chat_append(session_id: str, reply: str) -> None:
    """Push a complete message bubble as an 'append' event to stream_queues.

    Used for post-HITL responses when the streaming bubble no longer exists."""
    html = (
        '<div class="msg-agent msg-agent-right">'
        '<div class="msg-agent-body msg-agent-body-right">'
        '<div class="msg-agent-bubble">{}</div>'
        '<div class="msg-meta">CustomerClaw</div>'
        '</div>'
        '<div class="msg-avatar">✧</div>'
        '</div>'
    ).format(escape(reply))
    _ensure_stream_queue(session_id)
    await stream_queues[session_id].put({"event": "append", "data": html})


async def emit_stream_done(session_id: str, reply: str) -> None:
    """Push the final agent bubble HTML as a named 'done' event to stream_queues."""
    html = (
        '<div class="msg-agent-bubble">{}</div>'
        '<div class="msg-meta">CustomerClaw</div>'
    ).format(escape(reply))
    _ensure_stream_queue(session_id)
    await stream_queues[session_id].put({"event": "done", "data": html})


async def emit_stream_error(session_id: str) -> None:
    """Push an error-state bubble as 'done' so the pending bubble doesn't hang."""
    html = (
        '<div class="msg-agent-bubble" style="color:var(--rose-dim);font-style:italic;">'
        "Something went wrong, try again later."
        "</div>"
    )
    _ensure_stream_queue(session_id)
    await stream_queues[session_id].put({"event": "done", "data": html})


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
        response = await asyncio.to_thread(
            call_llm_streaming, session_id, history, TOOLS, loop
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
                args = json.loads(tc["function"]["arguments"])

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
                    await emit_stream_done(session_id, ack)
                    return  # halt — resumed via /actions/approve

        else:  # finish_reason == "stop"
            await emit_thought(session_id, "reason", "Composing reply…")
            db.append_message(session_id, "assistant", msg["content"])
            await emit_stream_done(session_id, msg["content"])
            await emit_chat_append(session_id, msg["content"])
            db.set_session_status(session_id, "DONE")
            return
