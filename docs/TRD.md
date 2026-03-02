# CustomerClaw — Technical Reference Document

## System Architecture

```mermaid
flowchart TB
    subgraph CLIENT["Operator Dashboard (Jinja2 + HTMX)"]
        direction LR
        CHAT["Chat Pane"]
        ELOG["Event Log"]
        INBOX["Inbox / Approvals / Orders / Rules"]
    end

    subgraph API["FastAPI Backend"]
        direction TB
        MSG["POST /chat/message"]
        SSE["GET /agent/thoughts/:id<br/><i>Server-Sent Events</i>"]
        APPROVE["POST /actions/approve/:id"]
        REJECT["POST /actions/reject/:id"]
        SETTINGS["GET · POST /settings"]
        RULES_EP["POST /rules/chat"]
    end

    subgraph AGENT["Agent Orchestrator — Plain While Loop"]
        direction TB
        LOOP["run_agent()"]
        LLM["call_llm()<br/><i>OpenAI SDK → OpenRouter</i>"]
        SAFE["SAFE Tools<br/>list_orders · check_order_status"]
        HITL["HITL Gate<br/>issue_refund · upsert_scheduled_task"]
        STREAM["emit_thought() → SSE Queue"]
        VALIDATE["validate_refund()<br/><i>Pre-flight + Approve-time</i>"]
    end

    subgraph RULES["Rules AI"]
        RULES_LLM["LLM with Rules System Prompt"]
        RULES_TOOLS["list · get · create/update · delete · toggle"]
    end

    subgraph POLLER["Proactive CRM Poller"]
        SCHED["APScheduler<br/><i>AsyncIOScheduler</i>"]
        CRON["Cron Jobs from<br/>scheduledTasks/*.json"]
        EXEC["execute_task()"]
    end

    subgraph DB["SQLite State Layer"]
        SESSIONS[("sessions")]
        ORDERS[("orders")]
        PENDING[("pending_actions")]
        THOUGHTS[("session_thoughts")]
        EVENTS[("order_events")]
        RCHAT[("rules_chat")]
    end

    %% Client → API
    CHAT -->|"form submit"| MSG
    ELOG -->|"SSE connect"| SSE
    INBOX -->|"Grant / Deny"| APPROVE & REJECT
    INBOX -->|"Rules chat"| RULES_EP

    %% API → Agent
    MSG -->|"BackgroundTask"| LOOP
    APPROVE -->|"TOCTOU re-validate"| VALIDATE
    APPROVE -->|"Resume agent"| LOOP
    REJECT -->|"Inject rejection → resume"| LOOP

    %% Agent internals
    LOOP --> LLM
    LLM -->|"finish_reason: tool_calls"| SAFE
    LLM -->|"finish_reason: tool_calls"| HITL
    LLM -->|"finish_reason: stop"| STREAM
    SAFE -->|"result → loop continues"| LOOP
    HITL -->|"PAUSED — awaits operator"| PENDING
    HITL -.->|"pre-flight check"| VALIDATE
    LOOP --> STREAM

    %% Rules AI
    RULES_EP --> RULES_LLM
    RULES_LLM --> RULES_TOOLS
    RULES_TOOLS -->|"write JSON + reload"| CRON

    %% Poller
    SCHED --> CRON
    CRON -->|"filter orders"| EXEC
    EXEC -->|"synthetic session"| LOOP

    %% SSE to client
    STREAM -->|"SSE push"| ELOG

    %% DB connections
    LOOP --- SESSIONS
    LOOP --- THOUGHTS
    SAFE --- ORDERS
    SAFE --- EVENTS
    HITL --- PENDING
    VALIDATE --- ORDERS
    EXEC --- ORDERS
    RULES_TOOLS --- RCHAT

    %% Styling
    classDef client fill:#1a1a2e,stroke:#c9b573,color:#e8dcc8
    classDef api fill:#16213e,stroke:#c9b573,color:#e8dcc8
    classDef agent fill:#0f3460,stroke:#9bb59b,color:#e8dcc8
    classDef rules fill:#1a1a2e,stroke:#b8a9c9,color:#e8dcc8
    classDef poller fill:#1a1a2e,stroke:#89a4c7,color:#e8dcc8
    classDef db fill:#1b1b2f,stroke:#c9b573,color:#c9b573
    classDef gate fill:#2d1b1b,stroke:#c97373,color:#e8dcc8

    class CLIENT client
    class API api
    class AGENT agent
    class RULES rules
    class POLLER poller
    class DB db
    class HITL,VALIDATE gate
```

## Core Flows

### 1. Reactive Chat — Customer Requests a Refund

```mermaid
sequenceDiagram
    actor Op as Operator
    participant UI as Dashboard (HTMX)
    participant API as FastAPI
    participant Agent as Agent Loop
    participant LLM as OpenRouter LLM
    participant DB as SQLite

    Op->>UI: Types "Refund order ORD-002"
    UI->>API: POST /chat/message
    API->>DB: append_message(user)
    API->>Agent: BackgroundTask → run_agent()
    API-->>UI: chat_exchange.html (pending=true)

    loop Agent Loop
        Agent->>DB: get_history()
        Agent->>LLM: chat.completions.create(tools)
        Agent-->>UI: emit_thought() → SSE stream

        alt finish_reason = tool_calls (SAFE)
            Agent->>DB: check_order_status() → query orders
            Agent->>DB: append_message(tool, result)
            Note over Agent: Loop continues
        else finish_reason = tool_calls (HITL)
            Agent->>DB: validate_refund() — pre-flight check
            Agent->>DB: save_pending_action()
            Agent->>DB: set_session_status(PAUSED)
            Agent-->>UI: emit_thought(hitl, "⚠ requires approval")
            Note over Agent: Loop exits — awaits operator
        else finish_reason = stop
            Agent->>DB: append_message(assistant, reply)
            Agent->>DB: set_session_status(DONE)
            Agent-->>UI: Stream final reply via SSE
        end
    end
```

### 2. HITL Approval with TOCTOU Guard

```mermaid
sequenceDiagram
    actor Op as Operator
    participant API as FastAPI
    participant DB as SQLite
    participant Agent as Agent Loop

    Note over Op,Agent: Session is PAUSED — action card visible

    Op->>API: POST /actions/approve/{session_id}

    API->>DB: validate_refund() — approve-time re-check

    alt Order state still valid
        API->>DB: set_session_status(RUNNING)
        API->>Agent: BackgroundTask → run_agent()
        Agent->>DB: execute issue_refund()
        Agent->>DB: update_order_status(refunded)
        Agent->>DB: log_order_event(refund_executed)
        API-->>Op: action_decision.html (approved)
    else Order state changed (TOCTOU race)
        API->>DB: delete_pending_action()
        API->>DB: append_message(tool, "Auto-rejected: ...")
        API->>DB: log_order_event(refund_auto_rejected)
        API->>DB: set_session_status(RUNNING)
        API->>Agent: BackgroundTask → run_agent() with error context
        API-->>Op: action_decision.html (rejected)
    end
```

### 3. Proactive Outreach via CRM Poller

```mermaid
sequenceDiagram
    participant Sched as APScheduler
    participant Poller as execute_task()
    participant DB as SQLite
    participant Agent as Agent Loop
    participant LLM as OpenRouter LLM

    Note over Sched: Cron trigger fires (server-local timezone)

    Sched->>Poller: execute_task(rule_config)
    Poller->>DB: query_orders_by_filters(status, hours, prefix)
    DB-->>Poller: [matching orders where outreached=0]

    loop Each matching order
        Poller->>DB: mark_order_outreached(order_id)
        Poller->>DB: log_order_event(outreach_triggered)
        Poller->>DB: get_or_create_session(proactive-{rule}-{order})
        Poller->>DB: append_raw_message(system instruction + identity override)
        Poller->>DB: set_session_status(RUNNING)
        Poller->>Agent: asyncio.create_task(run_agent())
        Agent->>LLM: Generates proactive outreach message
        Agent->>DB: append_message(assistant, outreach reply)
    end
```

### 4. Rules AI — Natural Language Rule Configuration

```mermaid
sequenceDiagram
    actor Op as Operator
    participant UI as Rules Tab
    participant API as POST /rules/chat
    participant AI as Rules AI Loop
    participant LLM as OpenRouter LLM
    participant FS as scheduledTasks/*.json
    participant Sched as APScheduler

    Op->>UI: "Create a rule for delayed orders every morning"
    UI->>API: POST /rules/chat
    API->>AI: run_rules_ai(message)

    loop Rules AI Tool Loop
        AI->>LLM: chat.completions.create(rules_tools)

        alt Tool call: list_rules
            LLM-->>AI: list_rules()
            AI->>FS: Read all *.json files
        else Tool call: create_or_update_rule
            LLM-->>AI: create_or_update_rule(config)
            AI->>FS: Write rule JSON
            AI->>Sched: reload_scheduler()
            Note over Sched: Hot-reload cron jobs
        end
    end

    AI-->>API: Final AI response
    API-->>UI: rules_chat_response.html + OOB rules_list refresh
```

## Order Status State Machine

```mermaid
stateDiagram-v2
    [*] --> processing

    processing --> shipped
    processing --> delayed
    processing --> cancelled
    processing --> refunded

    delayed --> shipped
    delayed --> cancelled
    delayed --> refunded

    shipped --> delivered
    shipped --> cancelled
    shipped --> refunded

    delivered --> refunded

    cancelled --> [*]
    refunded --> [*]

    note right of cancelled: Terminal state
    note right of refunded: Terminal state
```

## Tech Stack

| Layer | Technology | Why |
|---|---|---|
| **Frontend** | Jinja2 + HTMX | Server-rendered HTML fragments, zero JS framework overhead |
| **Real-time** | SSE (sse-starlette) | Simpler than WebSockets; HTMX handles reconnect natively |
| **Backend** | FastAPI | Async-first, BackgroundTasks for agent dispatch |
| **LLM** | OpenAI SDK → OpenRouter | Model-agnostic; swap models via settings without code changes |
| **Orchestrator** | Plain `while` loop | No framework — full control over tool dispatch and HITL gating |
| **State** | SQLite | Single-file DB for sessions, HITL queue, orders, event log |
| **Scheduler** | APScheduler (AsyncIO) | Cron-based proactive outreach, hot-reloadable from Rules AI |
| **Deployment** | Uvicorn | ASGI server with `--reload` for dev |

## Key Design Decisions

- **No agent framework** — the orchestrator is a 40-line `while` loop with explicit tool dispatch, not a LangChain/CrewAI abstraction. Every state transition is visible and debuggable.
- **Double-validation pattern** — refunds are validated both pre-flight (before HITL gate) and at approve-time (after operator clicks Grant) to prevent TOCTOU races.
- **Atomic HITL gate** — compare-and-swap on session status prevents double-approvals from concurrent clicks.
- **SSE dual-path rendering** — live events stream via SSE during agent runs; persisted events load from DB on page reload. Both converge into the same timeline format.
- **Rules AI as a meta-layer** — operators configure cron jobs through natural language, which writes JSON files and hot-reloads the scheduler. No restarts needed.
