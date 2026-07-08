"""Central configuration: loads .env and config/formula.yaml."""
from __future__ import annotations

import os
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

ET = ZoneInfo("America/New_York")


def today_et() -> date:
    """The MLB slate date. Runners are on UTC, where the calendar flips at
    8 PM ET — date.today() would target tomorrow's slate all evening."""
    return datetime.now(ET).date()

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
LOGS_DIR = ROOT / "logs"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "picks.db"
FORMULA_PATH = ROOT / "config" / "formula.yaml"

DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)


def env(key: str, default: str = "") -> str:
    val = os.getenv(key, default)
    if val in ("not_set", "set", "", "changeme"):
        return default
    return val


# --- API keys / secrets (all optional; system degrades gracefully) ----------
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
ODDS_API_KEY = env("ODDS_API_KEY")
WEATHER_API_KEY = env("WEATHER_API_KEY")
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")

# --- Model routing (hybrid strategy for cost control) ------------------------
# Opus: ONLY the final pick narratives (max picks/day calls).
# Sonnet: everything conversational (Telegram Q&A, /commands free text).
# All scripted work (fetching, scoring, SQLite, grading) is plain Python — $0.
NARRATIVE_MODEL = env("NARRATIVE_MODEL", "claude-opus-4-7")
CHAT_MODEL = env("CHAT_MODEL", "claude-sonnet-4-6")


@lru_cache(maxsize=1)
def formula() -> dict:
    """Load and cache the tunable scoring formula."""
    with open(FORMULA_PATH, "r") as fh:
        return yaml.safe_load(fh)


def reload_formula() -> dict:
    formula.cache_clear()
    return formula()
