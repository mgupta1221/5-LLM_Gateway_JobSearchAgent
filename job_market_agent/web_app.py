"""
web_app.py — FastAPI web application for India Job Market Intelligence.

Responsibilities:
  • Auto-start llm_gatewayV2 as a background subprocess (users run ONE command)
  • Serve the single-page UI (templates/index.html)
  • POST /analyze   → kick off an agent run, return job_id
  • GET  /stream/{job_id} → Server-Sent Events stream of live status events
  • GET  /result/{job_id} → fetch final result once done

Run:
    python web_app.py
    # or
    uvicorn web_app:app --reload --port 8000
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
_fh = logging.FileHandler(str(LOG_DIR / "agent_error.log"))
_fh.setLevel(logging.ERROR)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s — %(message)s", "%Y-%m-%d %H:%M:%S"))
logging.getLogger("agent_jobs").addHandler(_fh)
log = logging.getLogger("web_app")
log.addHandler(_fh)
log.setLevel(logging.ERROR)

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

# ── Load .env before importing anything that needs keys ──────────
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from config import LLM_GATEWAY_DIR, LLM_GATEWAY_URL
from schemas import JobQuery
from agent_jobs import run as run_agent


# ────────────────────────────────────────────────────────────────
# Gateway lifecycle
# ────────────────────────────────────────────────────────────────

_gateway_proc: Optional[subprocess.Popen] = None


def _kill_port(port: int) -> None:
    """Kill any process occupying the given port (Windows + Unix)."""
    import platform
    try:
        if platform.system() == "Windows":
            out = subprocess.check_output(
                ["netstat", "-ano"], text=True, stderr=subprocess.DEVNULL
            )
            killed = set()
            for line in out.splitlines():
                if f":{port}" not in line:
                    continue
                parts = line.split()
                pid = parts[-1]
                if pid.isdigit() and int(pid) not in (0, os.getpid()) and pid not in killed:
                    subprocess.call(
                        ["powershell", "-Command", f"Stop-Process -Id {pid} -Force"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    killed.add(pid)
                    print(f"   killed old gateway process (PID {pid})")
        else:
            subprocess.call(
                ["fuser", "-k", f"{port}/tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except Exception as e:
        print(f"   (could not auto-kill port {port}: {e})")


def _start_gateway() -> Optional[subprocess.Popen]:
    """Always (re)start llm_gatewayV2 so the latest .env is loaded."""
    gateway_dir = Path(LLM_GATEWAY_DIR)

    if not gateway_dir.exists():
        print(f"⚠️  llm_gatewayV2 not found at {gateway_dir}")
        return None

    READY_URL = f"{LLM_GATEWAY_URL}/v1/providers"
    PORT = int(LLM_GATEWAY_URL.split(":")[-1].rstrip("/"))

    # Kill any existing process on the gateway port so fresh config loads
    print(f"🔄 Restarting llm_gatewayV2 on port {PORT} (ensures latest config)...")
    _kill_port(PORT)
    time.sleep(1)   # give OS time to release the port

    print("🚀 Starting llm_gatewayV2 on port 8100...")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "main:app",
            "--host", "0.0.0.0", "--port", "8100",
            "--log-level", "warning",
        ],
        cwd=str(gateway_dir),
        env={**os.environ},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait up to 20 s for the gateway to become ready
    for i in range(40):
        # Check if subprocess crashed
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode("utf-8", errors="replace")
            print(f"❌ llm_gatewayV2 crashed on startup:\n{stderr[:800]}")
            return None

        try:
            r = httpx.get(READY_URL, timeout=1)
            if r.status_code == 200:
                print("✅ llm_gatewayV2 started successfully")
                return proc
        except Exception:
            pass

        if i % 4 == 0:          # print a dot every 2 s so user sees progress
            print(f"   waiting for gateway... ({i//2}s)", flush=True)
        time.sleep(0.5)

    print("⚠️  Gateway did not respond in 20 s — proceeding anyway")
    return proc


# ────────────────────────────────────────────────────────────────
# FastAPI app
# ────────────────────────────────────────────────────────────────

# Active jobs: job_id → {"queue": asyncio.Queue, "result": dict | None}
_active_jobs: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _gateway_proc
    _gateway_proc = _start_gateway()
    yield
    if _gateway_proc:
        _gateway_proc.terminate()
        print("🛑 llm_gatewayV2 stopped")


app = FastAPI(
    title="India Job Market Intelligence",
    description="Multi-LLM agent for Indian tech job market analysis",
    lifespan=lifespan,
)


# ────────────────────────────────────────────────────────────────
# Routes
# ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    html = Path(__file__).parent / "templates" / "index.html"
    return HTMLResponse(html.read_text(encoding="utf-8"))


class AnalyzeRequest(BaseModel):
    role:             str
    city:             str
    experience_years: int


@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    job_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _active_jobs[job_id] = {"queue": queue, "result": None}

    query = JobQuery(
        role=req.role,
        city=req.city,
        experience_years=req.experience_years,
    )

    async def _run():
        try:
            result = await run_agent(query, queue)
            _active_jobs[job_id]["result"] = result
        except Exception as exc:
            log.exception("Agent run FAILED for job %s", job_id)
            await queue.put({
                "type":    "error",
                "icon":    "❌",
                "message": f"Agent error: {exc}",
            })

    asyncio.create_task(_run())
    return {"job_id": job_id}


@app.get("/stream/{job_id}")
async def stream_status(job_id: str):
    if job_id not in _active_jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    queue = _active_jobs[job_id]["queue"]

    async def _generator():
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=120)
                yield {"data": json.dumps(event)}
                if event.get("type") in ("done", "error"):
                    break
            except asyncio.TimeoutError:
                yield {"data": json.dumps({"type": "ping", "message": "..."})}

    return EventSourceResponse(_generator())


@app.get("/result/{job_id}")
async def get_result(job_id: str):
    if job_id not in _active_jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    result = _active_jobs[job_id].get("result")
    if result is None:
        raise HTTPException(status_code=202, detail="Still processing")
    return result


@app.post("/validate-prompt")
async def validate_prompt():
    """Evaluate SYSTEM_PROMPT against the 8-criteria rubric via the gateway."""
    from prompts import SYSTEM_PROMPT

    rubric = (
        "You are a Prompt Evaluation Assistant. Evaluate the system prompt below against 8 criteria. "
        "Return ONLY a JSON object — no other text.\n\n"
        "CRITERIA (each must be true or false):\n"
        "1. explicit_reasoning — Instructs step-by-step reasoning before every action?\n"
        "2. structured_output — Enforces a strict output format (FUNCTION_CALL / FINAL_ANSWER with JSON schema)?\n"
        "3. tool_separation — Clearly separates WHEN to call tools vs WHEN to reason, with a multi-turn sequence?\n"
        "4. conversation_loop — Shows a multi-turn conversation flow with at least 2 turns?\n"
        "5. instructional_framing — Includes a complete worked example with input, reasoning, tool calls, and output?\n"
        "6. internal_self_checks — Requires explicit validation checks (numbered, with PASS/FAIL) on tool results before using them?\n"
        "7. reasoning_type_awareness — Defines and requires reasoning type labels ([LOOKUP], [ARITHMETIC], [LOGIC], [VERIFY]) on every step?\n"
        "8. fallbacks — Defines specific FAILURE → ACTION recovery pairs for every failure mode?\n\n"
        "Add an 'overall_clarity' string summarising your assessment.\n\n"
        f"SYSTEM PROMPT TO EVALUATE:\n--- START ---\n{SYSTEM_PROMPT}\n--- END ---"
    )

    try:
        r = httpx.post(
            f"{LLM_GATEWAY_URL}/v1/chat",
            json={"prompt": rubric, "temperature": 0, "max_tokens": 600},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        text = data.get("text", "")
        provider = data.get("provider", "unknown")
        model = data.get("model", "unknown")

        # Extract JSON from response
        start = text.find("{")
        end = text.rfind("}") + 1
        result = {}
        if start >= 0 and end > start:
            import json as _json
            try:
                result = _json.loads(text[start:end])
            except Exception:
                result = {"raw": text}
        else:
            result = {"raw": text}

        return {"result": result, "provider": provider, "model": model}
    except Exception as exc:
        raise HTTPException(502, f"Validation failed: {exc}")


@app.get("/health")
async def health():
    """Used by the gateway start-up check and the UI status indicator."""
    gw_ok = False
    try:
        r = httpx.get(f"{LLM_GATEWAY_URL}/health", timeout=2)
        gw_ok = r.status_code == 200
    except Exception:
        pass
    return {"status": "ok", "gateway": "up" if gw_ok else "down"}


# ────────────────────────────────────────────────────────────────
# Entrypoint
# ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print("═" * 60)
    print("  India Job Market Intelligence")
    print("  http://localhost:8000")
    print("═" * 60)
    uvicorn.run("web_app:app", host="0.0.0.0", port=8000, reload=False)
