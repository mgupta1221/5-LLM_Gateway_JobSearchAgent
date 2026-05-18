# India Job Market Intelligence Agent

A multi-LLM AI agent that analyzes the Indian tech job market in real time. Enter a job role, city, and experience level — the agent queries live job boards, extracts salary benchmarks, and delivers a data-driven market report with salary ranges, top skills, and hiring companies.

Built on **llm_gatewayV2** (a FastAPI-based LLM router) with automatic provider failover, MCP tools, Pydantic models, and a web UI with live Server-Sent Events (SSE) streaming.

## Demo Video

[Watch the project demo](https://youtu.be/VOXoppJuwak)

## Project Structure

```
LLM_Gateway project/
├── llm_gatewayV2/              # LLM Gateway — multi-provider router
│   ├── main.py                 # FastAPI server (port 8100)
│   ├── providers.py            # Provider adapters (Anthropic, Gemini, Groq, etc.)
│   ├── router.py               # Smart routing with rate limits & cooldowns
│   ├── client.py               # Python client SDK (used by the agent)
│   ├── cache.py                # Response caching (Gemini context caching)
│   ├── db.py                   # SQLite call logging
│   ├── schemas.py              # Request/response Pydantic models
│   ├── static/                 # Gateway dashboard UI
│   └── .env                    # LLM provider API keys
│
├── job_market_agent/           # The AI agent application
│   ├── web_app.py              # FastAPI web server (port 8000) — auto-starts gateway
│   ├── agent_jobs.py           # Agent loop (executor + verifier)
│   ├── mcp_server_jobs.py      # MCP tool server (4 tools)
│   ├── prompts.py              # System prompt (passes 8-criteria rubric)
│   ├── schemas.py              # Pydantic models (JobQuery, MarketVerdict, etc.)
│   ├── config.py               # Centralized config from .env
│   ├── validate_prompt.py      # Standalone prompt rubric validator
│   ├── templates/index.html    # Single-page web UI
│   ├── logs/                   # Error-only logs (agent_error.log)
│   └── .env                    # Job market API keys (Adzuna, Tavily)
│
└── README.md
```

## Features

- **Multi-LLM with automatic failover** — Anthropic (Claude via Azure AI Foundry) → Cerebras → Gemini → OpenRouter → Groq. If one provider fails, the gateway retries with the next.
- **Live SSE streaming** — every agent step (LLM calls, tool invocations, failovers, verification) streams to the browser in real time.
- **MCP tool server** — 4 tools exposed via Model Context Protocol (stdio transport):
  - `search_jobs` — Adzuna India API for live job listings
  - `extract_salary_benchmark` — Tavily web search for salary data (AmbitionBox, PayScale)
  - `compute_market_stats` — percentiles, skill demand frequency, confidence scoring
  - `report_insufficient_data` — structured fallback when data is too thin
- **Executor + Verifier pattern** — executor runs fast (reasoning=off), verifier cross-checks with structured output (reasoning=medium) returning a typed `MarketVerdict`.
- **Pydantic everywhere** — `JobQuery`, `MarketVerdict`, `AgentTrace`, `ToolDef`, `MarketStats` for all data boundaries.
- **Prompt evaluation** — built-in 8-criteria rubric validator accessible from the UI ("Validate Prompt" button) and as a standalone script.
- **Gateway dashboard** — real-time provider status, call logs, rate-limit tracking at `http://localhost:8100`.

## How the Agent Loop Works

```
User submits query (role, city, experience)
        │
        ▼
┌──────────────────────────────────┐
│  MCP Server starts (stdio)       │
│  → Registers 4 tools             │
└──────────────┬───────────────────┘
               │
        ┌──────▼──────┐
        │  EXECUTOR    │  (up to 8 turns, reasoning=off)
        │  ────────    │
        │  1. LLM receives system prompt + tools + user query
        │  2. LLM returns FUNCTION_CALL or FINAL_ANSWER
        │  3. If FUNCTION_CALL → dispatch tool via MCP
        │  4. Tool result → append to messages → back to step 1
        │  5. If FINAL_ANSWER → break loop
        └──────┬──────┘
               │
        ┌──────▼──────┐
        │  VERIFIER    │  (single LLM call, reasoning=medium)
        │  ─────────   │
        │  Cross-checks the executor's report:
        │  • salary_realistic?  • sample_size_ok?
        │  • skills_coherent?   • data_confidence?
        │  Returns typed MarketVerdict (Pydantic)
        └──────┬──────┘
               │
               ▼
        Final result → SSE "done" event → UI renders report
```

Every step emits status events to an `asyncio.Queue`, which the web layer streams as SSE to the browser. Failovers, provider switches, and errors are all surfaced live.

## LLM Gateway (llm_gatewayV2)

The gateway is a standalone FastAPI service that abstracts away LLM provider differences:

- **Smart routing** — respects per-provider rate limits (RPM/RPD/TPM), applies cooldowns on errors, and skips providers that lack required capabilities (tools, reasoning, structured output).
- **Backoff & recovery** — 429s, 5xx errors, and timeouts trigger timed cooldowns; the router auto-recovers when cooldowns expire.
- **Provider adapters** — Anthropic (with Azure AI Foundry support), Gemini, Cerebras, Groq, OpenRouter, NVIDIA, GitHub Models. Each adapter normalizes to a common request/response format.
- **Caching** — Gemini context caching for repeated system prompts.
- **Call logging** — every request logged to SQLite with latency, tokens, provider, status.
- **Dashboard** — visual overview of provider health, failover order, and recent calls.

## Setup & Run

### 1. Clone and install dependencies

```bash
cd job_market_agent
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt

cd ../llm_gatewayV2
pip install -r requirements.txt
```

### 2. Configure environment files

**`llm_gatewayV2/.env`** — LLM provider keys and gateway settings:

```env
LLM_ORDER=anthropic,cerebras,gemini,openrouter,groq

ANTHROPIC_API_KEY=your-key-here
ANTHROPIC_MODEL=claude-haiku-4-5-TalkToData
ANTHROPIC_BASE_URL=https://your-resource.services.ai.azure.com/anthropic

CEREBRAS_API_KEY=your-key-here
CEREBRAS_MODEL=llama-3.3-70b

GEMINI_API_KEY=your-key-here
GEMINI_MODEL=gemini-2.5-flash

OPEN_ROUTER_API_KEY=your-key-here
OPENROUTER_MODEL=meta-llama/llama-3.3-70b-instruct:free

GROQ_API_KEY=your-key-here
GROQ_MODEL=llama-3.3-70b-versatile

GATEWAY_V2_PORT=8100
PROVIDER_TIMEOUT=35
```

**`job_market_agent/.env`** — job market API keys:

```env
ADZUNA_APP_ID=your-app-id
ADZUNA_APP_KEY=your-app-key
TAVILY_API_KEY=your-tavily-key
LLM_GATEWAY_V2_URL=http://localhost:8100
```

### 3. Run the application

```bash
cd job_market_agent
python web_app.py
```

This single command:
1. Starts (or restarts) llm_gatewayV2 on port 8100
2. Launches the web app on port 8000

Open **http://localhost:8000** in your browser.

### 4. Optional: validate the system prompt

Click the **"Validate Prompt"** button in the web UI, or run standalone:

```bash
cd job_market_agent
python validate_prompt.py
```

This evaluates the system prompt against 8 criteria: explicit reasoning, structured output, tool separation, conversation loop, instructional framing, internal self-checks, reasoning type awareness, and fallbacks.

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Web UI |
| `/analyze` | POST | Start analysis — returns `{job_id}` |
| `/stream/{job_id}` | GET | SSE stream of live agent events |
| `/result/{job_id}` | GET | Fetch final result |
| `/validate-prompt` | POST | Run 8-criteria prompt evaluation |
| `/health` | GET | App + gateway health check |
