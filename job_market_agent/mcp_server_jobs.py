"""
mcp_server_jobs.py — MCP server exposing 4 Job Market Intelligence tools.

Tools:
  search_jobs               → Adzuna India API  (live job listings)
  extract_salary_benchmark  → Tavily search     (salary from AmbitionBox / PayScale)
  compute_market_stats      → pure Python math  (percentiles, skill demand, confidence)
  report_insufficient_data  → structured fallback when data is too thin

Run standalone for a sanity check:
    python mcp_server_jobs.py
"""

import json
import os
import re
import sys
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

# Keys come from config (which loads .env)
sys.path.insert(0, str(Path(__file__).parent))
from config import (
    ADZUNA_APP_ID, ADZUNA_APP_KEY, ADZUNA_BASE_URL,
    TAVILY_API_KEY, TAVILY_SEARCH_URL,
)

mcp = FastMCP("job-market-server")


# ────────────────────────────────────────────────────────────────
# Skill taxonomy — used by search_jobs and compute_market_stats
# ────────────────────────────────────────────────────────────────

TECH_SKILLS = [
    # Languages
    "Python", "Java", "JavaScript", "TypeScript", "Go", "Rust", "C++", "C#",
    "Kotlin", "Swift", "Ruby", "PHP", "Scala", "R",
    # Frameworks / libs
    "React", "Angular", "Vue", "Node.js", "Django", "FastAPI", "Flask",
    "Spring Boot", "Spring", "Express", ".NET", "Laravel",
    # Cloud / Infra
    "AWS", "GCP", "Azure", "Docker", "Kubernetes", "Terraform", "Ansible",
    "CI/CD", "Jenkins", "GitHub Actions",
    # Data / AI
    "Machine Learning", "Deep Learning", "TensorFlow", "PyTorch",
    "scikit-learn", "Pandas", "NumPy", "Spark", "Hadoop", "Airflow",
    "MLOps", "LLM", "NLP", "Computer Vision",
    # Databases
    "PostgreSQL", "MySQL", "MongoDB", "Redis", "Elasticsearch",
    "Cassandra", "DynamoDB", "BigQuery", "Snowflake",
    # Messaging / APIs
    "Kafka", "RabbitMQ", "REST", "GraphQL", "gRPC", "Microservices",
    # Practices
    "SQL", "NoSQL", "Linux", "Git", "Agile", "Scrum", "DevOps",
    "System Design", "Data Structures",
]


def _extract_skills(text: str) -> list[str]:
    t = text.lower()
    return [s for s in TECH_SKILLS if s.lower() in t][:12]


def _parse_lpa(text: str) -> list[float]:
    """Extract LPA salary figures from free text."""
    salaries: list[float] = []
    patterns = [
        # "6 - 22 LPA" / "6 to 22 lakhs" etc.
        r'(\d+(?:\.\d+)?)\s*(?:to|-|–)\s*(\d+(?:\.\d+)?)\s*(?:lpa|lakh|l\b|lac)',
        # "14 LPA" / "14L" / "14 lakhs"
        r'(\d+(?:\.\d+)?)\s*(?:lpa|lakh|l\b|lac)',
        # "₹14,00,000" → convert to LPA
        r'₹\s*(\d+)[,\d]*',
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            for g in m.groups():
                if g:
                    try:
                        v = float(g)
                        # If looks like full rupees (>1000), convert to LPA
                        if v > 200:
                            v = round(v / 100_000, 1)
                        if 2.0 <= v <= 100.0:
                            salaries.append(v)
                    except ValueError:
                        pass
    return list(set(salaries))


# ────────────────────────────────────────────────────────────────
# Tool 1 — search_jobs
# ────────────────────────────────────────────────────────────────

@mcp.tool()
def search_jobs(role: str, city: str, experience_years: int) -> str:
    """Search live job listings in India via the Adzuna API.

    Returns JSON with total listing count and per-listing details including
    title, company, salary_lpa (when disclosed), skills, and location.
    Call this tool to understand the DEMAND side of the market.
    """
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        return json.dumps({
            "error": "Adzuna API keys not configured (ADZUNA_APP_ID / ADZUNA_APP_KEY)",
            "listings": [],
            "total": 0,
        })

    try:
        r = httpx.get(
            ADZUNA_BASE_URL,
            params={
                "app_id":           ADZUNA_APP_ID,
                "app_key":          ADZUNA_APP_KEY,
                "results_per_page": 20,
                "what":             role,
                "where":            city,
                "content-type":     "application/json",
            },
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()

        listings = []
        for job in data.get("results", []):
            sal_min = job.get("salary_min") or 0
            sal_max = job.get("salary_max") or 0
            salary_lpa = None
            if sal_min > 0 and sal_max > 0:
                salary_lpa = round((sal_min + sal_max) / 2 / 100_000, 1)
            elif sal_min > 0:
                salary_lpa = round(sal_min / 100_000, 1)

            desc = (job.get("description") or "") + " " + (job.get("title") or "")
            skills = _extract_skills(desc)

            listings.append({
                "title":       job.get("title", ""),
                "company":     (job.get("company") or {}).get("display_name", ""),
                "salary_lpa":  salary_lpa,
                "skills":      skills,
                "location":    (job.get("location") or {}).get("display_name", city),
                "posted_date": job.get("created", "")[:10],
            })

        return json.dumps({
            "total":    len(listings),
            "listings": listings,
            "city":     city,
            "role":     role,
        })

    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"Adzuna HTTP {e.response.status_code}", "listings": [], "total": 0})
    except Exception as e:
        return json.dumps({"error": str(e), "listings": [], "total": 0})


# ────────────────────────────────────────────────────────────────
# Tool 2 — extract_salary_benchmark
# ────────────────────────────────────────────────────────────────

@mcp.tool()
def extract_salary_benchmark(role: str, city: str) -> str:
    """Extract salary benchmark data for a role in an Indian city via Tavily.

    Queries AmbitionBox, PayScale India, and similar salary sites and parses
    the salary ranges from the extracted text.
    Call this tool to understand the BENCHMARK / SUPPLY side of the market.
    """
    if not TAVILY_API_KEY:
        return json.dumps({
            "error": "Tavily API key not configured (TAVILY_API_KEY)",
            "min_lpa": None, "max_lpa": None, "median_lpa": None,
            "sample_size": 0, "source": "not configured",
        })

    query = (
        f"{role} salary in {city} India LPA CTC per annum 2024 2025 "
        f"site:ambitionbox.com OR site:payscale.com OR site:glassdoor.co.in OR site:naukri.com"
    )

    try:
        r = httpx.post(
            TAVILY_SEARCH_URL,
            json={
                "api_key":       TAVILY_API_KEY,
                "query":         query,
                "search_depth":  "basic",
                "include_answer": True,
                "max_results":   5,
            },
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()

        combined = ""
        if data.get("answer"):
            combined += data["answer"] + " "
        for result in data.get("results", []):
            combined += (result.get("content") or "") + " "

        salaries = _parse_lpa(combined)

        if salaries:
            salaries_sorted = sorted(salaries)
            n = len(salaries_sorted)
            return json.dumps({
                "min_lpa":    salaries_sorted[0],
                "max_lpa":    salaries_sorted[-1],
                "median_lpa": salaries_sorted[n // 2],
                "sample_size": n,
                "source":     "AmbitionBox / PayScale via Tavily",
                "raw_text":   combined[:600],
            })
        else:
            return json.dumps({
                "min_lpa":    None,
                "max_lpa":    None,
                "median_lpa": None,
                "sample_size": 0,
                "source":     "Tavily search returned no parseable salary figures",
                "raw_text":   combined[:300],
            })

    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"Tavily HTTP {e.response.status_code}", "sample_size": 0})
    except Exception as e:
        return json.dumps({"error": str(e), "sample_size": 0})


# ────────────────────────────────────────────────────────────────
# Tool 3 — compute_market_stats
# ────────────────────────────────────────────────────────────────

@mcp.tool()
def compute_market_stats(
    listings_json: str,
    benchmark_json: str,
    experience_years: int,
) -> str:
    """Compute comprehensive market statistics from job listings and salary benchmark.

    Calculates median salary, P25/P75 percentiles, top skills by demand frequency,
    top hiring companies, experience premium, and a data confidence score.
    This is pure computation — no external API call.
    """
    try:
        listings_data = json.loads(listings_json) if isinstance(listings_json, str) else listings_json
        benchmark     = json.loads(benchmark_json) if isinstance(benchmark_json, str) else benchmark_json

        listing_items: list[dict] = (
            listings_data.get("listings", [])
            if isinstance(listings_data, dict)
            else (listings_data if isinstance(listings_data, list) else [])
        )

        # ── Salary pool ─────────────────────────────────────────
        salary_pool: list[float] = []
        for job in listing_items:
            s = job.get("salary_lpa")
            if s and 2.0 <= float(s) <= 100.0:
                salary_pool.append(float(s))

        if isinstance(benchmark, dict):
            for key in ("min_lpa", "median_lpa", "max_lpa"):
                v = benchmark.get(key)
                if v and 2.0 <= float(v) <= 100.0:
                    salary_pool.append(float(v))

        # ── Salary statistics ────────────────────────────────────
        if salary_pool:
            ss = sorted(salary_pool)
            n  = len(ss)
            median_lpa = ss[n // 2]
            p25        = ss[max(0, n // 4)]
            p75        = ss[min(n - 1, 3 * n // 4)]
            min_lpa    = ss[0]
            max_lpa    = ss[-1]
        else:
            median_lpa = p25 = p75 = min_lpa = max_lpa = 0.0

        # ── Experience premium (15 % per year above 2-yr baseline) ─
        exp_premium = max(0.0, (experience_years - 2) * 15.0)

        # ── Skill demand ─────────────────────────────────────────
        skill_count: dict[str, int] = {}
        for job in listing_items:
            for sk in job.get("skills", []):
                skill_count[sk] = skill_count.get(sk, 0) + 1

        total = max(len(listing_items), 1)
        top_skills = [
            {
                "skill":        s,
                "demand_count": c,
                "demand_pct":   round(c / total * 100),
            }
            for s, c in sorted(skill_count.items(), key=lambda x: -x[1])[:8]
        ]

        # ── Top companies ─────────────────────────────────────────
        co_count: dict[str, int] = {}
        for job in listing_items:
            co = (job.get("company") or "").strip()
            if co:
                co_count[co] = co_count.get(co, 0) + 1

        top_companies = [
            {"name": co, "openings": cnt}
            for co, cnt in sorted(co_count.items(), key=lambda x: -x[1])[:5]
        ]

        # ── Data confidence ───────────────────────────────────────
        if len(listing_items) >= 10 and len(salary_pool) >= 5:
            confidence = "high"
        elif len(listing_items) >= 5 and len(salary_pool) >= 3:
            confidence = "medium"
        else:
            confidence = "low"

        return json.dumps({
            "median_salary_lpa":    round(median_lpa, 1),
            "salary_range":         {"min": round(min_lpa, 1), "max": round(max_lpa, 1)},
            "percentile_25_lpa":    round(p25, 1),
            "percentile_75_lpa":    round(p75, 1),
            "top_skills":           top_skills,
            "top_companies":        top_companies,
            "total_listings":       len(listing_items),
            "salary_data_points":   len(salary_pool),
            "experience_premium_pct": round(exp_premium, 1),
            "data_confidence":      confidence,
        })

    except Exception as e:
        return json.dumps({"error": str(e)})


# ────────────────────────────────────────────────────────────────
# Tool 4 — report_insufficient_data
# ────────────────────────────────────────────────────────────────

@mcp.tool()
def report_insufficient_data(reason: str, partial_data_json: str) -> str:
    """Report when available data is too thin for a reliable analysis.

    Call this tool when:
    - job listing count < 5, OR
    - salary data points < 3, OR
    - both search_jobs and extract_salary_benchmark returned errors.

    Returns a structured report so the agent can still produce a useful response.
    """
    try:
        partial = json.loads(partial_data_json) if partial_data_json else {}
    except Exception:
        partial = {}

    return json.dumps({
        "status":         "INSUFFICIENT_DATA",
        "reason":         reason,
        "partial_data":   partial,
        "recommendation": (
            "Try a more common role title (e.g. 'Python Developer' instead of "
            "'Python Backend Engineer') or a larger city such as Bangalore, "
            "Mumbai, or Hyderabad."
        ),
    })


# ────────────────────────────────────────────────────────────────
# Entrypoint — run as MCP stdio server
# ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
