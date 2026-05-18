"""
config.py — Single source of truth for all API keys and settings.
All keys are loaded from .env in this project folder.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from this project folder
load_dotenv(Path(__file__).parent / ".env")

# ─── LLM Gateway ────────────────────────────────────────────────
# Provider keys live in llm_gatewayV2/.env — not needed here
LLM_GATEWAY_URL = os.getenv("LLM_GATEWAY_V2_URL", "http://localhost:8100")
LLM_GATEWAY_DIR = str(Path(__file__).parent.parent / "llm_gatewayV2")

# ─── Job Market APIs ────────────────────────────────────────────
ADZUNA_APP_ID  = os.getenv("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# ─── Adzuna India endpoint ──────────────────────────────────────
ADZUNA_BASE_URL = "https://api.adzuna.com/v1/api/jobs/in/search/1"

# ─── Tavily endpoint ────────────────────────────────────────────
TAVILY_SEARCH_URL  = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"

# ─── Agent settings ─────────────────────────────────────────────
EXECUTOR_PROVIDER = None        # None = gateway auto-failover
EXECUTOR_REASONING = "off"      # keep executor fast
VERIFIER_REASONING = "medium"   # verifier gets more budget
MAX_AGENT_TURNS = 8

# ─── Indian city name normalisation ─────────────────────────────
CITY_ALIASES: dict[str, str] = {
    "bengaluru": "Bangalore",
    "bangalore": "Bangalore",
    "bombay":    "Mumbai",
    "mumbai":    "Mumbai",
    "delhi":     "Delhi",
    "new delhi": "Delhi",
    "ncr":       "Delhi",
    "hyderabad": "Hyderabad",
    "hyd":       "Hyderabad",
    "pune":      "Pune",
    "chennai":   "Chennai",
    "madras":    "Chennai",
    "noida":     "Noida",
    "gurgaon":   "Gurgaon",
    "gurugram":  "Gurgaon",
    "kolkata":   "Kolkata",
    "calcutta":  "Kolkata",
}


def normalise_city(city: str) -> str:
    return CITY_ALIASES.get(city.strip().lower(), city.strip().title())
