# CustomerClaw User Acceptance Testing (UAT) Plan

This document outlines the User Acceptance Testing (UAT) cases for the CustomerClaw application based on the requirements defined in `CLAUDE.md`.

## Test Environment Setup
- **Server:** FastAPI app running locally on `http://localhost:8000`
- **Database:** Local SQLite database initialized with mock orders and seeded data.
- **LLM:** OpenRouter API configured in the environment (`OPENROUTER_API_KEY`) or in the app's settings.

---

## 🧪 UAT Case 1: General Chat and Basic Response
**Objective:** Verify that the agent can answer basic non-actionable queries and updates the UI correctly.
**Steps:**
1. Navigate to `http://localhost:8000/`. (The app should redirect to a new `/chat/{session_id}` route).
2. Look at the Scrying Glass (thought trace). It should be empty or show an initialization state.
3. Type `"Hello, who are you?"` in the chat input and submit.
**Expected Results:**
- The Scrying Glass shows a `supervisor` trace ("Evaluating conversation…") followed by a `reason` trace ("Composing reply…").
- The agent responds with a greeting identifying itself as a customer support assistant.
- The chat bubble history updates immediately.

---

## 🧪 UAT Case 2: Safe Tool Execution (Order Status Lookup)
**Objective:** Verify that the agent can use `SAFE_TOOLS` (e.g., `check_order_status`) without requiring operator approval.
**Steps:**
1. In an active chat session, type: `"What is the status of order ORD-001?"` and submit.
**Expected Results:**
- The Scrying Glass shows an `execute` trace (e.g., `→ check_order_status`).
- The agent responds with the status of `ORD-001` (e.g., "processing" or "delayed").
- No action cards appear in the Pending Seals pane.

---

## 🧪 UAT Case 3: HITL Tool Trigger (Refund Request)
**Objective:** Verify that sensitive actions (e.g., `issue_refund`) trigger the Human-in-the-Loop (HITL) gate and pause the agent.
**Steps:**
1. In an active chat session, type: `"I am very unhappy! I'd like a refund for order ORD-001."` and submit.
**Expected Results:**
- The Scrying Glass shows a `hitl` trace (e.g., `⚠ issue_refund requires approval`).
- The chat bubble indicates "awaiting the seal" or shows a pending state.
- A new action card appears in the **Pending Seals** (Action Queue) pane containing the tool name (`issue_refund`) and arguments (e.g., `order_id="ORD-001"`, amount).
- The **Seals** tab badge updates in real-time to show the pending count (e.g., "1 awaiting") without requiring the operator to click the tab.
- The agent pauses and waits for operator input.

---

## 🧪 UAT Case 4: HITL Action Approval (Grant)
**Objective:** Verify that granting a pending action resumes the agent and executes the tool.
**Steps:**
1. Continue from **UAT Case 3** where a refund request is pending.
2. In the Pending Seals pane, locate the action card for `ORD-001` and click **Grant**.
**Expected Results:**
- The action card updates its visual state to "approved" (`action_decision.html`).
- The backend flips the session status from `PAUSED` back to `RUNNING`.
- The Scrying Glass shows the agent resuming execution.
- The agent sends a confirmation message in the chat that the refund was processed.

---

## 🧪 UAT Case 5: HITL Action Rejection (Deny)
**Objective:** Verify that rejecting a pending action cancels the tool execution and prompts the agent to respond accordingly.
**Steps:**
1. Submit a new sensitive request: `"Please refund order ORD-002 as well."`
2. Wait for the `hitl` trace and the new action card in the Pending Seals pane.
3. In the Pending Seals pane, click **Deny**.
**Expected Results:**
- The action card updates its visual state to "rejected".
- A system message "Action rejected by operator." is injected into the context.
- The agent resumes execution and formulates a response declining the refund politely to the user.

---

## 🧪 UAT Case 6: Settings Modal Configuration
**Objective:** Verify that the operator can modify LLM settings at runtime.
**Steps:**
1. In the Operator's Sanctum UI, click the gear icon to open the settings modal.
2. Verify the modal displays the current `model` and API key status (`has_key`).
3. Change the model to `anthropic/claude-3.5-sonnet` and optionally enter an OpenRouter API key.
4. Submit the form.
**Expected Results:**
- The modal form submits via POST to `/settings` and closes.
- Subsequent chat messages utilize the newly selected model (observable in backend logs or by improved response quality).

---

## 🧪 UAT Case 7: Proactive CRM Poller
**Objective:** Verify that the background APScheduler detects stale orders and initiates a proactive session.
**Steps:**
1. Ensure the SQLite database has an order with a `delayed` status older than 24 hours.
2. Wait for the polling interval (60 seconds) or manually invoke `poll_crm()` during development.
**Expected Results:**
- A new session starts with ID `proactive-{order_id}`.
- The agent generates a synthetic message drafting a proactive outreach to the customer.
- The new session appears in the Inbox pane (or session list) for the operator to review.

---

## 🧪 UAT Case 8: Error Handling (Error Toast)
**Objective:** Verify that agent execution errors gracefully display in the UI without crashing the application.
**Steps:**
1. Temporarily disable the internet connection or use an invalid `OPENROUTER_API_KEY`.
2. Send a message in the chat.
**Expected Results:**
- The agent fails to reach the LLM API.
- An `error_toast.html` overlay appears in the top-right corner with a rose palette styling, displaying the error message.
- The toast auto-dismisses after 5 seconds.
