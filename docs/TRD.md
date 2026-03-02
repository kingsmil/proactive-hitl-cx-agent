# CustomerClaw — Technical Reference Document

## System Architecture

```mermaid
flowchart LR
    subgraph UI["👤 Operator Dashboard"]
        direction TB
        CHAT["Chat Pane"]
        APPROVE["Approvals"]
        RULES_UI["Rules Config"]
    end

    subgraph BACKEND["⚙️ FastAPI"]
        direction TB
        API["REST + SSE Endpoints"]
    end

    subgraph CORE["🧠 Agent Orchestrator"]
        direction TB
        LOOP["while loop"]
        LLM["LLM<br/><i>OpenRouter</i>"]
        LOOP -->|"prompt + history"| LLM

        LLM -->|"tool_calls"| TOOLS
        LLM -->|"stop"| REPLY["Stream Reply<br/>via SSE"]

        TOOLS{"Safe or<br/>Dangerous?"}
        TOOLS -->|"✅ Safe"| SAFE["Execute Immediately<br/><i>order lookup</i>"]
        TOOLS -->|"⚠️ Dangerous"| HITL["HITL Gate<br/><i>refunds</i>"]

        SAFE -->|"result"| LOOP
        HITL -->|"PAUSE + wait"| PENDING["Pending Action"]
    end

    subgraph PROACTIVE["⏰ CRM Poller"]
        SCHED["APScheduler<br/><i>cron jobs</i>"]
        RULES_AI["Rules AI<br/><i>NL → cron config</i>"]
        RULES_AI -->|"write + reload"| SCHED
    end

    DB[("SQLite<br/><i>sessions · orders<br/>events · actions</i>")]

    %% Main flows
    CHAT -->|"message"| API
    API -->|"BackgroundTask"| LOOP
    REPLY -->|"SSE"| UI
    PENDING -->|"action card"| APPROVE
    APPROVE -->|"grant / deny"| API
    API -->|"resume"| LOOP
    RULES_UI --> RULES_AI
    SCHED -->|"synthetic session"| LOOP

    %% DB connections
    CORE <-..-> DB
    PROACTIVE <-..-> DB
```

> **Reading the diagram:** A customer message enters from the left, flows through
> FastAPI into the agent's `while` loop. The LLM decides to either call a **safe tool**
> (executes instantly, loops back) or a **dangerous tool** (pauses for human approval).
> The operator approves/denies from the dashboard, and the loop resumes.
> Separately, the **CRM Poller** fires cron jobs that inject synthetic sessions
> into the same agent loop — configurable via the **Rules AI** chat interface.

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
