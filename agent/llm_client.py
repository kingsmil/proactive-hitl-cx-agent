import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

import certifi
import db

log = logging.getLogger("agent")
_ssl_ctx = ssl.create_default_context(cafile=certifi.where())

# ---------------------------------------------------------------------------
# LLM Constants
# ---------------------------------------------------------------------------

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_URL     = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

DEFAULT_MODEL        = "bytedance-seed/seed-1.6-flash"
GEMINI_DEFAULT_MODEL = "gemini-2.0-flash"

SYSTEM_PROMPT = """You are CustomerClaw, a concise customer-support assistant for an e-commerce platform.

Guidelines:
- **CRITICAL**: The first thing you must do when a customer asks about an order is to ask for their phone number for verification. Do not proceed until you have their phone number.
- Always call check_order_status before making any decisions about an order. Make sure to provide the phone number they gave you.
- **MANDATORY**: When a customer requests a refund, you MUST call the `issue_refund` tool. NEVER respond with only a text message promising or confirming a refund — you do not have the ability to process refunds without the tool. If the customer says "I want a refund", "please refund me", "can I get my money back", or anything similar, you MUST call `issue_refund` with the correct order_id, customer_phone, amount, and reason. Skipping the tool call means the refund will NOT be processed.
- Do NOT call issue_refund if the order is already in 'cancelled' state; instead, inform the customer that their refund will be automatically credited.
- Never promise to perform an action (refund, status change, etc.) without actually calling the corresponding tool. If you cannot call the tool, explain why.
- Be brief and empathetic. One short paragraph per reply.
- Never invent order details — only use what the tools return.
- When you have order details, reference the customer by name and mention the product they ordered.
- If the user hasn't asked about a specific order, you may still need to ask for their phone number first, and then use list_orders to see what orders are available and suggest a few the user might want to ask about."""

# Shared text injected into proactive sessions so the agent skips the
# phone-number verification step (identity is already known from context).
PROACTIVE_IDENTITY_OVERRIDE = (
    "IMPORTANT: You already have the customer's identity from the context above. "
    "Do NOT ask for their phone number. Instead, greet them by name "
    "and proceed directly with the outreach message."
)

# System prompt variant for proactive outreach sessions — replaces the
# phone-verification instruction with the identity override.
PROACTIVE_SYSTEM_PROMPT = SYSTEM_PROMPT.replace(
    "**CRITICAL**: The first thing you must do when a customer asks about an "
    "order is to ask for their phone number for verification. Do not proceed "
    "until you have their phone number.",
    "**CRITICAL**: This is a proactive outreach session. "
    + PROACTIVE_IDENTITY_OVERRIDE,
)

# ---------------------------------------------------------------------------
# LLM helpers — shared config, request building, streaming
# ---------------------------------------------------------------------------

def _build_llm_endpoint_config():
    """Resolve which LLM provider to use and return (url, model, headers)."""
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
        api_key = (db.get_setting("openrouter_api_key", "")
                   or os.environ.get("OPENROUTER_API_KEY", ""))
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer {}".format(api_key),
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": "CustomerClaw",
        }

    return url, model, headers


def _sanitize_message(msg: dict) -> dict:
    """Strip non-standard fields from a message dict before sending to the LLM.

    Some providers (e.g. Alibaba/Qwen via OpenRouter) reject requests that
    contain extra keys like ``timestamp`` or ``is_manual``.  This keeps only
    the keys that the OpenAI chat-completions schema expects.
    """
    ALLOWED_KEYS = {"role", "content", "tool_calls", "tool_call_id", "name"}
    clean = {k: v for k, v in msg.items() if k in ALLOWED_KEYS}

    # Coerce content to a string if it's somehow a dict/list (some providers
    # reject non-string content with a confusing "got an object" error).
    content = clean.get("content")
    if content is not None and not isinstance(content, str):
        clean["content"] = json.dumps(content) if isinstance(content, (dict, list)) else str(content)

    # Ensure content is a string for roles that require it.
    # assistant messages may legitimately have content=None when tool_calls
    # are present, but user/system/tool messages must always be strings.
    if clean.get("role") in ("user", "system", "tool") and clean.get("content") is None:
        clean["content"] = ""

    # Tool messages MUST have a tool_call_id per the OpenAI spec.
    # If missing, convert to a system message to preserve the information
    # without breaking the conversation structure (dropping would orphan
    # the preceding assistant tool_call message).
    if clean.get("role") == "tool" and not clean.get("tool_call_id"):
        log.warning("Converting tool message with no tool_call_id to system: %s", clean.get("content", "")[:100])
        clean["role"] = "system"
        clean.pop("tool_call_id", None)

    return clean


def _sanitize_history(history: list[dict]) -> list[dict]:
    """Sanitize a full message history, dropping irrecoverably malformed entries."""
    result = []
    for msg in history:
        cleaned = _sanitize_message(msg)
        if cleaned is not None:
            result.append(cleaned)
    return result


def _build_llm_request_payload(model, history, tools, stream=False, system_prompt=None):
    """Build the JSON request body for the LLM API call."""
    sanitized_history = _sanitize_history(history)
    body_dict = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt or SYSTEM_PROMPT}] + sanitized_history,
        "tools": tools,
        "tool_choice": "auto",
    }
    if stream:
        body_dict["stream"] = True
    payload = json.dumps(body_dict).encode("utf-8")
    return body_dict, payload


def _accumulate_tool_call_argument_deltas(tool_calls_by_index, delta_tool_calls):
    """Merge incremental tool-call chunks into a consolidated dict keyed by index."""
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
    """Convert accumulated streaming state into a response dict matching call_llm's format."""
    msg = {"role": "assistant", "content": text_content or None}
    if tool_calls_by_index:
        msg["tool_calls"] = [
            tool_calls_by_index[i] for i in sorted(tool_calls_by_index)
        ]
    return {"choices": [{"finish_reason": finish_reason, "message": msg}]}


def _execute_llm_request_with_retry(url, headers, payload):
    """Send a non-streaming POST and return the parsed JSON response."""
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

def call_llm(history, tools, system_prompt=None):
    """POST to OpenRouter or Gemini and return the parsed response dict."""
    url, model, headers = _build_llm_endpoint_config()
    body_dict, payload = _build_llm_request_payload(model, history, tools, system_prompt=system_prompt)
    log.debug("LLM REQUEST  → %s\n%s", url, json.dumps(body_dict, indent=2))
    response = _execute_llm_request_with_retry(url, headers, payload)
    log.debug("LLM RESPONSE ←\n%s", json.dumps(response, indent=2))
    return response


def call_llm_with_custom_prompt(system_prompt: str, history: list, tools: list):
    """POST with a custom system prompt instead of the default SYSTEM_PROMPT."""
    return call_llm(history, tools, system_prompt=system_prompt)


def call_llm_streaming(history, tools, push_chunk_callback: Optional[Callable[[str], None]] = None, system_prompt=None):
    """POST with stream=True, pushing each token to a callback.
    Returns an assembled response dict in the same shape as call_llm."""
    url, model, headers = _build_llm_endpoint_config()
    body_dict, payload = _build_llm_request_payload(model, history, tools, stream=True, system_prompt=system_prompt)
    log.debug("LLM REQUEST (stream) → %s\n%s", url, json.dumps(body_dict, indent=2))

    for attempt in range(3):
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            text_content, tool_calls_by_index, finish_reason = \
                _read_streaming_response_chunks(req, push_chunk_callback)

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


def _read_streaming_response_chunks(req, push_chunk_callback: Optional[Callable[[str], None]]):
    """Open the HTTP stream and process each SSE line as it arrives."""
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

            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta  = choice.get("delta", {})
            fr     = choice.get("finish_reason")
            if fr:
                finish_reason = fr

            # Stream text token to browser via callback in real time
            token = delta.get("content") or ""
            if token:
                text_content += token
                if push_chunk_callback:
                    push_chunk_callback(token)

            # Accumulate tool-call fragments
            if delta.get("tool_calls"):
                _accumulate_tool_call_argument_deltas(
                    tool_calls_by_index, delta["tool_calls"]
                )

    return text_content, tool_calls_by_index, finish_reason
