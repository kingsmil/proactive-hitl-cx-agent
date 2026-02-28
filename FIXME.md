# UI Persistence & Event Log — TODO / FIXME

## Issue 1: Stale "processing…" bubble ✅ FIXED
**Fix applied**: `chat_pane.html` — processing bubble now only renders when:
- Last history message is from `user` → shows streaming bubble (chunks + done)  
- Last history message is nonexistent or from `tool`/`system` → shows thinking indicator
- Last history message is from `assistant` → shows **nothing** (agent finished)

## Issue 2: Approvals and Orders tabs broken 🔍 NEEDS VERIFICATION
**Suspected cause**: Routes `/actions/pending` and `/orders` exist and look correct.
May have been a server startup failure (data/ dir missing after reset_db.sh).
After recreating `data/` dir and restarting server, these may now work.
Needs a manual test click in the browser.

## Issue 3: "Send Message" form does not work 🔍 NEEDS VERIFICATION
**Suspected cause**: Server startup was failing (DB couldn't open). Server is now running.
Try sending a message via the Inbox form and see if it works.

## Issue 4: Tool calls visible in event log ✅ FIXED
**Fix applied**: `get_event_log` in `chat.py` now parses:
- `role=assistant` messages with `tool_calls` → "tool_call" events with tool name + args
- `role=tool` messages → "tool_result" events (truncated at 200 chars)

## Issue 5: Post-HITL reply glitch ✅ FIXED
**Root cause**: After HITL approval, the streaming bubble (`#reply-body-{session_id}`) no
longer exists in the DOM (it was replaced by the ack message). `emit_stream_done` targets
that element, so the post-approval reply was silently dropped by HTMX.
**Fix**: `from_hitl` flag in `_run_agent_body` — when resuming from HITL, uses
`emit_chat_append` (appends a full bubble to commune-content) instead.

## Issue 6: Garbage tokens during intermediate LLM passes ✅ FIXED
**Root cause**: `push_chunk` streamed tokens from ALL LLM calls (including tool-selecting
intermediate calls), causing garbled partial text to appear in the streaming bubble.
**Fix**: `_suppress_stream` flag — token streaming is disabled for:
  - Post-HITL resumes (no bubble)
  - Intermediate tool-call passes
  Re-enabled before the final stop-reply pass.

## Issue 7: Event Log UI rearchitected ✅ DONE
**Change**: Old dot/line timeline replaced with the same compact thought-entry row style
used by the live SSE log. All events (order lifecycle, messages, tool calls, thoughts)
render as `node-tag pill + text` rows — unified look with the live feed.
