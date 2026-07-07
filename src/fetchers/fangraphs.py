"""FanGraphs — secondary cross-check for pitcher K% (30-day and season).

The MLB Stats API is the primary source for every projection input; FanGraphs
is fetched best-effort through its public leaders JSON endpoint and only used
to sanity-check the primary K% numbers. Any failure here is non-fatal.
"""
from __future__ import annotations

import unicodedata
from datetime import date, timedelta
from typing import Optional

from fetchers import fetch_cached, get_with_retry

API = "https://www.fangraphs.com/api/leaders/major-league/data"


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return s.lower().replace(".", "").strip()


def _fetch_k_board(season: int, start: Optional[str] = None,
                   end: Optional[str] = None) -> Optional[dict]:
    params = {
        "age": "", "pos": "all", "stats": "pit", "lg": "all", "qual": "0",
        "season": season, "season1": season, "ind": "0", "team": "0",
        "pageitems": "2000", "pagenum": "1", "type": "8",
    }
    if start and end:
        params.update({"month": "1000", "startdate": start, "enddate": end})
    r = get_with_retry(API, params=params, timeout=30,
                       headers={"Accept": "application/json"})
    if r is None:
        return None
    try:
        rows = r.json().get("data", [])
    except Exception:
        return None
    out = {}
    for row in rows:
        name = row.get("PlayerName") or row.get("Name") or ""
        k = row.get("K%")
        if name and isinstance(k, (int, float)):
            out[_norm(name)] = float(k)  # already a 0-1 fraction in the API
    return out or None


def k_pct(name: str, season: Optional[int] = None,
          last_days: Optional[int] = None) -> tuple[Optional[float], bool]:
    """(K% fraction, fresh) for a pitcher by name. None when unavailable."""
    season = season or date.today().year
    if last_days:
        start = (date.today() - timedelta(days=last_days)).isoformat()
        end = date.today().isoformat()
        cache_name = f"fangraphs_k_{season}_{last_days}d"
        board, fresh = fetch_cached(
            cache_name, lambda: _fetch_k_board(season, start, end))
    else:
        board, fresh = fetch_cached(
            f"fangraphs_k_{season}", lambda: _fetch_k_board(season))
    if not board:
        return None, fresh
    return board.get(_norm(name)), fresh
