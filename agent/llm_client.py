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

DEFAULT_MODEL        = "openai/gpt-oss-120b"
GEMINI_DEFAULT_MODEL = "gemini-2.0-flash"

SYSTEM_PROMPT = """You are CustomerClaw, a concise customer-support assistant for an e-commerce platform.

Guidelines:
- **CRITICAL**: The first thing you must do when a customer asks about an order is to ask for their phone number for verification. Do not proceed until you have their phone number.
- Always call check_order_status before making any decisions about an order. Make sure to provide the phone number they gave you.
- Call issue_refund only when the customer is clearly owed a refund and you have confirmed the order status matches what they say. Do NOT call issue_refund if the order is already in 'cancelled' state; instead, inform the customer that their refund will be automatically credited.
- Be brief and empathetic. One short paragraph per reply.
- Never invent order details — only use what the tools return.
- When you have order details, reference the customer by name and mention the product they ordered.
- If the user hasn't asked about a specific order, you may still need to ask for their phone number first, and then use list_orders to see what orders are available and suggest a few the user might want to ask about."""

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


def _build_llm_request_payload(model, history, tools, stream=False, system_prompt=None):
    """Build the JSON request body for the LLM API call."""
    body_dict = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt or SYSTEM_PROMPT}] + history,
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
    url, model, headers = _build_llm_endpoint_config()
    body_dict = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}] + history,
        "tools": tools,
        "tool_choice": "auto",
    }
    payload = json.dumps(body_dict).encode("utf-8")
    log.debug("LLM REQUEST (custom) → %s\n%s", url, json.dumps(body_dict, indent=2))
    response = _execute_llm_request_with_retry(url, headers, payload)
    log.debug("LLM RESPONSE (custom) ←\n%s", json.dumps(response, indent=2))
    return response


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

            choice = chunk.get("choices", [{}])[0]
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
