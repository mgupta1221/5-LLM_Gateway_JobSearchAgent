"""
agent_jobs.py — Job Market Intelligence Agent loop.

Mirrors the agent5.py (Session 5) pattern:
  ┌─────────────────────────────────────────────┐
  │  agent5.py              agent_jobs.py        │
  │  ─────────────────      ─────────────────    │
  │  arithmetic task    →   India job market     │
  │  add / subtract     →   search_jobs /        │
  │    tools                extract_salary /     │
  │                         compute_stats        │
  │  Verdict(float)     →   MarketVerdict        │
  │  reasoning="off"    →   same (executor)      │
  │  reasoning="medium" →   same (verifier)      │
  │  provider=None      →   same (auto-failover) │
  └─────────────────────────────────────────────┘

Key upgrades over agent5.py for this project:
  • status_queue — every significant event is pushed so the web layer
    can stream live notifications to the browser via SSE.
  • _tool_status_message — human-readable description per tool call.
  • verify_market_report — verifier uses structured output (Pydantic
    MarketVerdict) with reasoning="medium", identical pattern to agent5.py.
"""

import asyncio
import functools
import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Any

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    filename=str(LOG_DIR / "agent_error.log"),
    level=logging.ERROR,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("agent_jobs")

from pydantic import BaseModel, Field

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# llm_gatewayV2 client (same import trick as agent5.py)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "llm_gatewayV2"))
from client import LLM  # noqa: E402

from config import EXECUTOR_PROVIDER, EXECUTOR_REASONING, VERIFIER_REASONING, MAX_AGENT_TURNS
from prompts import SYSTEM_PROMPT
from schemas import AgentTrace, JobQuery, MarketVerdict, ToolDef


# ────────────────────────────────────────────────────────────────
# MCP ↔ Gateway bridge (identical to agent5.py)
# ────────────────────────────────────────────────────────────────

def mcp_tool_to_v2(t) -> dict:
    return ToolDef(
        name=t.name,
        description=t.description or "",
        input_schema=t.inputSchema or {"type": "object", "properties": {}},
    ).model_dump()


# ────────────────────────────────────────────────────────────────
# Parallel MCP dispatcher (identical to agent5.py)
# ────────────────────────────────────────────────────────────────

async def dispatch_tool_calls(session: ClientSession, tool_calls: list[dict]) -> list[dict]:
    async def run_one(tc: dict) -> dict:
        try:
            result = await session.call_tool(tc["name"], tc.get("arguments") or {})
            text = result.content[0].text if result.content else ""
            return {
                "role":        "tool",
                "tool_call_id": tc["id"],
                "tool_name":   tc["name"],
                "content":     text,
            }
        except Exception:
            log.exception("Tool %s FAILED", tc["name"])
            return {
                "role":        "tool",
                "tool_call_id": tc["id"],
                "tool_name":   tc["name"],
                "content":     json.dumps({"error": traceback.format_exc()}),
            }

    tasks = [asyncio.create_task(run_one(tc)) for tc in tool_calls]
    return [await t for t in tasks]


# ────────────────────────────────────────────────────────────────
# Status helpers
# ────────────────────────────────────────────────────────────────

def _tool_label(name: str, args: dict) -> str:
    return {
        "search_jobs": (
            f"Searching '{args.get('role','')}' jobs in "
            f"{args.get('city','')} via Adzuna India..."
        ),
        "extract_salary_benchmark": (
            f"Extracting salary benchmarks for '{args.get('role','')}' "
            f"in {args.get('city','')} via Tavily → AmbitionBox..."
        ),
        "compute_market_stats": (
            "Computing salary percentiles, skill demand frequency "
            "and market confidence score..."
        ),
        "report_insufficient_data": "Reporting data insufficiency...",
    }.get(name, f"Calling {name}...")


async def _emit(q: asyncio.Queue, **kw) -> None:
    await q.put(kw)


# ────────────────────────────────────────────────────────────────
# Agent executor loop — reasoning="off"  (fast, tool-dispatch)
# ────────────────────────────────────────────────────────────────

async def _run_executor(
    session:   ClientSession,
    tools:     list[dict],
    user_task: str,
    trace:     AgentTrace,
    status_q:  asyncio.Queue,
    provider:  str | None,
) -> str:
    llm = LLM()
    messages: list[dict] = [{"role": "user", "content": user_task}]

    for turn in range(1, MAX_AGENT_TURNS + 1):

        await _emit(status_q,
            type="llm_thinking",
            icon="🧠",
            message=f"Agent reasoning (turn {turn} of max {MAX_AGENT_TURNS})...",
            turn=turn,
        )

        try:
            loop = asyncio.get_running_loop()
            reply = await loop.run_in_executor(
                None,
                functools.partial(
                    llm.chat,
                    messages=messages,
                    system=SYSTEM_PROMPT,
                    cache_system=True,
                    tools=tools,
                    tool_choice="auto",
                    reasoning=EXECUTOR_REASONING,
                    provider=provider,
                    temperature=0,
                    max_tokens=2048,
                ),
            )
        except Exception:
            log.exception("Executor LLM call FAILED on turn %d", turn)
            raise

        provider_used = reply["provider"]
        model_used    = reply["model"]
        attempted     = reply.get("attempted") or []

        # ── Detect and surface LLM failover ──────────────────────
        if attempted:
            failed_parts = []
            for a in attempted:
                reason = a.get("reason", "")
                if "timed out" in reason or "408" in reason:
                    tag = "timed out"
                elif "401" in reason or "403" in reason or "auth" in reason.lower():
                    tag = "auth error"
                elif "failed" in reason:
                    tag = reason.split("failed:")[-1].strip()[:60]
                else:
                    tag = reason[:60] if reason else "unavailable"
                failed_parts.append(f"{a['provider']} ({tag})")
            await _emit(status_q,
                type="failover",
                icon="🔄",
                message=(
                    f"{', '.join(failed_parts)} — "
                    f"switching to {provider_used} ({model_used})"
                ),
                provider=provider_used,
                model=model_used,
            )
        else:
            await _emit(status_q,
                type="provider",
                icon="✨",
                message=f"Using {provider_used} · {model_used}",
                provider=provider_used,
                model=model_used,
                latency_ms=reply["latency_ms"],
            )

        trace.add(
            kind="llm_call",
            turn=turn,
            provider=provider_used,
            model=model_used,
            latency_ms=reply["latency_ms"],
            input_tokens=reply["input_tokens"],
            output_tokens=reply["output_tokens"],
            cache_read=reply.get("cache_read_input_tokens"),
            cache_create=reply.get("cache_creation_input_tokens"),
            dialect=reply.get("tool_call_dialect"),
            text=reply.get("text"),
            payload={"tool_calls": reply.get("tool_calls", [])},
        )

        tool_calls = reply.get("tool_calls") or []

        # ── No tool calls → executor has a final answer ───────────
        if not tool_calls:
            return reply.get("text", "").strip()

        # ── Echo assistant turn back into history ─────────────────
        messages.append({
            "role":       "assistant",
            "content":    reply.get("text", "") or "",
            "tool_calls": tool_calls,
        })

        # ── Emit tool-call status events ──────────────────────────
        for tc in tool_calls:
            await _emit(status_q,
                type="tool_call",
                icon="🔧",
                message=_tool_label(tc["name"], tc.get("arguments", {})),
                tool_name=tc["name"],
            )

        # ── Dispatch tool calls (parallel via TaskGroup) ──────────
        results = await dispatch_tool_calls(session, tool_calls)

        for tc, r in zip(tool_calls, results):
            preview = r["content"]
            if len(preview) > 120:
                preview = preview[:120] + "…"
            await _emit(status_q,
                type="tool_result",
                icon="✅",
                message=f"Got data from {tc['name']}",
                tool_name=tc["name"],
                preview=preview,
            )
            trace.add(
                kind="tool_call",
                turn=turn,
                tool_name=tc["name"],
                tool_args=tc.get("arguments"),
                tool_result=r["content"],
            )

        messages.extend(results)

    raise RuntimeError(f"Agent exceeded max_turns={MAX_AGENT_TURNS}")


# ────────────────────────────────────────────────────────────────
# Verifier — separate LLM call, reasoning="medium", Pydantic output
# (identical pattern to agent5.py verify())
# ────────────────────────────────────────────────────────────────

async def verify_market_report(
    trace:           AgentTrace,
    executor_answer: str,
    query:           JobQuery,
) -> tuple[MarketVerdict, str, str]:
    """Independent verification pass — uses structured output so the model
    returns a validated MarketVerdict. reasoning='medium' gives it budget
    to catch subtle issues (e.g. senior-role bias inflating the median)."""

    tool_results = {
        e.tool_name: e.tool_result
        for e in trace.events
        if e.kind == "tool_call" and e.tool_result
    }

    schema = MarketVerdict.model_json_schema()
    llm    = LLM()

    loop = asyncio.get_running_loop()
    reply = await loop.run_in_executor(
        None,
        functools.partial(
            llm.chat,
            prompt=(
                f"You are a market data verifier for the Indian job market.\n\n"
                f"Query: {query.role} in {query.city}, "
                f"{query.experience_years} years experience.\n\n"
                f"Agent's final answer:\n{executor_answer}\n\n"
                f"Raw tool results:\n"
                f"{json.dumps({k: (v or '')[:400] for k, v in tool_results.items()}, indent=2)}\n\n"
                "Verify the following:\n"
                "1. Is the median salary realistic for India (₹2L–₹80L)?\n"
                "2. Is the sample size sufficient (≥5 listings)?\n"
                "3. Are the top skills coherent with the stated role?\n"
                "4. Does the final answer match what the tools actually returned?\n\n"
                "Return a single MarketVerdict JSON object."
            ),
            system="You are a precise market data verifier. Return only a MarketVerdict.",
            cache_system=True,
            response_format={
                "type":   "json_schema",
                "schema": schema,
                "name":   "MarketVerdict",
                "strict": True,
            },
            reasoning=VERIFIER_REASONING,
            temperature=0,
            max_tokens=1024,
        ),
    )

    verifier_provider = reply.get("provider", "unknown")
    verifier_model    = reply.get("model", "unknown")

    if reply.get("parsed"):
        return MarketVerdict.model_validate(reply["parsed"]), verifier_provider, verifier_model

    # Fallback — structured output not honoured; build from raw answer
    try:
        raw = executor_answer
        if "FINAL_ANSWER:" in raw:
            raw = raw.split("FINAL_ANSWER:")[1].strip()
        d = json.loads(raw)
    except Exception:
        d = {}

    return MarketVerdict(
        passed=bool(d.get("median_salary_lpa")),
        data_confidence="low",
        salary_realistic=True,
        sample_size_ok=False,
        skills_coherent=True,
        reason="Structured output not returned by verifier — using fallback.",
        final_median_lpa=float(d.get("median_salary_lpa", 0)),
        salary_range_lpa=d.get("salary_range", {"min": 0.0, "max": 0.0}),
        top_skills=[
            (s["skill"] if isinstance(s, dict) else s)
            for s in d.get("top_skills", [])[:5]
        ],
        top_companies=[
            (c["name"] if isinstance(c, dict) else c)
            for c in d.get("top_companies", [])[:3]
        ],
        recommendation="Data quality uncertain — treat as indicative only.",
    ), verifier_provider, verifier_model


# ────────────────────────────────────────────────────────────────
# Public entry point
# ────────────────────────────────────────────────────────────────

async def run(query: JobQuery, status_queue: asyncio.Queue) -> dict:
    """Run the full agent pipeline and stream status events to status_queue.

    Returns a result dict with keys: query, verdict, final_answer,
    trace_summary, raw_answer.
    """

    user_task = (
        f"Analyse the Indian job market for: {query.role} in {query.city}. "
        f"Experience level: {query.experience_years} year(s). "
        f"Find current salary ranges (in LPA), top required skills, and top hiring companies. "
        f"IMPORTANT: Call search_jobs AND extract_salary_benchmark in parallel in your first turn."
    )

    await _emit(status_queue,
        type="status",
        icon="🚀",
        message=(
            f"Starting analysis for {query.role} in {query.city} "
            f"({query.experience_years} yr experience)..."
        ),
    )

    await _emit(status_queue,
        type="status",
        icon="⚙️",
        message="Launching MCP tools server (search_jobs, extract_salary_benchmark, compute_market_stats)...",
    )

    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).with_name("mcp_server_jobs.py"))],
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = (await session.list_tools()).tools
            tools = [mcp_tool_to_v2(t) for t in mcp_tools]

            await _emit(status_queue,
                type="status",
                icon="🛠️",
                message=f"Tools ready: {', '.join(t.name for t in mcp_tools)}",
            )

            trace = AgentTrace(goal=user_task, query=query)

            try:
                # ── Execute ──────────────────────────────────────
                answer_text = await _run_executor(
                    session, tools, user_task, trace, status_queue,
                    provider=EXECUTOR_PROVIDER,
                )
            except Exception as exc:
                log.exception("Executor failed")
                err_msg = str(exc)
                if "503" in err_msg:
                    err_msg = "All LLM providers unavailable (503). Check API keys and rate limits in llm_gatewayV2/.env"
                await _emit(status_queue,
                    type="error", icon="❌",
                    message=f"Executor error: {err_msg}",
                )
                return {"query": query.model_dump(), "error": err_msg,
                        "trace_summary": trace.summary()}

            await _emit(status_queue,
                type="status",
                icon="🔍",
                message="Running independent verification (reasoning=medium)...",
            )

            try:
                # ── Verify ───────────────────────────────────────
                verdict, v_provider, v_model = await verify_market_report(trace, answer_text, query)
            except Exception as exc:
                log.exception("Verifier failed")
                await _emit(status_queue,
                    type="status", icon="⚠️",
                    message=f"Verifier failed ({exc}) — skipping verification",
                )
                verdict = MarketVerdict(
                    passed=False, data_confidence="low",
                    salary_realistic=True, sample_size_ok=False,
                    skills_coherent=True,
                    reason=f"Verifier error: {exc}",
                    final_median_lpa=0, salary_range_lpa={"min": 0, "max": 0},
                    top_skills=[], top_companies=[],
                    recommendation="Verification unavailable — treat results as unverified.",
                )
                v_provider, v_model = "none", "none"

            trace.add(kind="verdict", turn=0, payload=verdict.model_dump())

            await _emit(status_queue,
                type="provider",
                icon="🔬",
                message=f"Verifier using {v_provider}",
                provider=v_provider,
                model=v_model,
            )

            await _emit(status_queue,
                type="verdict",
                icon="✅" if verdict.passed else "⚠️",
                message=(
                    f"Verification {'passed' if verdict.passed else 'flagged'} — "
                    f"confidence: {verdict.data_confidence.upper()}"
                ),
                passed=verdict.passed,
                confidence=verdict.data_confidence,
            )

            # ── Parse FINAL_ANSWER from executor text ─────────────
            final_data: dict = {}
            try:
                chunk = answer_text
                if "FINAL_ANSWER:" in chunk:
                    chunk = chunk.split("FINAL_ANSWER:")[1].strip()
                brace = chunk.find("{")
                if brace >= 0:
                    chunk = chunk[brace:]
                final_data = json.loads(chunk)
            except Exception:
                final_data = {}

            summary = trace.summary()
            result  = {
                "query":         query.model_dump(),
                "verdict":       verdict.model_dump(),
                "final_answer":  final_data,
                "trace_summary": summary,
                "raw_answer":    answer_text,
            }

            await _emit(status_queue,
                type="done",
                icon="🎉",
                message="Analysis complete!",
                result=result,
            )

            return result
