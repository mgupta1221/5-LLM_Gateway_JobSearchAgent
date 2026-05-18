"""
schemas.py — All Pydantic models for the Job Market Intelligence Agent.
Single source of truth for every data boundary in the system.
"""
import time
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ────────────────────────────────────────────────────────────────
# Request / Input
# ────────────────────────────────────────────────────────────────

class JobQuery(BaseModel):
    """User's analysis request — validated at the web layer."""
    role: str
    city: str
    experience_years: int = Field(ge=0, le=30)


# ────────────────────────────────────────────────────────────────
# Tool definitions (MCP ↔ Gateway bridge)
# ────────────────────────────────────────────────────────────────

class ToolDef(BaseModel):
    """Canonical tool envelope — what llm_gatewayV2 expects."""
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


# ────────────────────────────────────────────────────────────────
# Job data models
# ────────────────────────────────────────────────────────────────

class JobListing(BaseModel):
    title: str
    company: str
    salary_lpa: Optional[float] = None
    skills: list[str] = Field(default_factory=list)
    location: str = ""
    posted_date: str = ""


class SalaryBenchmark(BaseModel):
    min_lpa: Optional[float] = None
    max_lpa: Optional[float] = None
    median_lpa: Optional[float] = None
    sample_size: int = 0
    source: str = ""
    raw_text: str = ""


class SkillDemand(BaseModel):
    skill: str
    demand_count: int
    demand_pct: float


class CompanyOpenings(BaseModel):
    name: str
    openings: int


class MarketStats(BaseModel):
    median_salary_lpa: float
    salary_range: dict[str, float]
    percentile_25_lpa: float
    percentile_75_lpa: float
    top_skills: list[SkillDemand]
    top_companies: list[CompanyOpenings]
    total_listings: int
    salary_data_points: int
    experience_premium_pct: float
    data_confidence: Literal["high", "medium", "low"]


# ────────────────────────────────────────────────────────────────
# Verifier output — typed Pydantic contract
# ────────────────────────────────────────────────────────────────

class MarketVerdict(BaseModel):
    """Verifier's typed output — returned via response_format structured output."""
    passed: bool
    data_confidence: Literal["high", "medium", "low"]
    salary_realistic: bool
    sample_size_ok: bool
    skills_coherent: bool
    reason: str
    final_median_lpa: float
    salary_range_lpa: dict[str, float]
    top_skills: list[str]
    top_companies: list[str]
    recommendation: str


# ────────────────────────────────────────────────────────────────
# Agent trace — structured event log (mirrors agent5.py pattern)
# ────────────────────────────────────────────────────────────────

class TraceEvent(BaseModel):
    kind: Literal["llm_call", "tool_call", "verdict", "status"]
    turn: int
    provider: Optional[str] = None
    model: Optional[str] = None
    latency_ms: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read: Optional[int] = None
    cache_create: Optional[int] = None
    dialect: Optional[str] = None
    tool_name: Optional[str] = None
    tool_args: Optional[dict] = None
    tool_result: Optional[str] = None
    text: Optional[str] = None
    payload: Optional[dict] = None


class AgentTrace(BaseModel):
    goal: str
    query: JobQuery
    events: list[TraceEvent] = Field(default_factory=list)
    started_at: float = Field(default_factory=time.time)

    def add(self, **kw) -> None:
        self.events.append(TraceEvent(**kw))

    def summary(self) -> dict:
        llm_calls  = [e for e in self.events if e.kind == "llm_call"]
        tool_calls = [e for e in self.events if e.kind == "tool_call"]
        return {
            "llm_turns":       len(llm_calls),
            "tool_calls":      len(tool_calls),
            "total_in_tokens": sum(e.input_tokens  or 0 for e in llm_calls),
            "total_out_tokens":sum(e.output_tokens or 0 for e in llm_calls),
            "cache_reads":     sum(e.cache_read    or 0 for e in llm_calls),
            "wall_clock_s":    round(time.time() - self.started_at, 2),
            "providers_used":  list({e.provider for e in llm_calls if e.provider}),
        }
