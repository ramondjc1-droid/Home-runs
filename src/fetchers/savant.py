"""Baseball Savant — CSW% and whiff% via the custom-leaderboard CSV export.

Enrichment only: the projection runs without it. CSW% is used as a
regression check — a pitcher whose K% outruns his CSW% is a fade candidate,
so the csw_aligns_with_k confidence point only fires when Savant agrees.
"""
from __future__ import annotations

import csv
import io
from datetime import date
from typing import Optional

from fetchers import fetch_cached, get_with_retry

URL = "https://baseballsavant.mlb.com/leaderboard/custom"


def _fetch_leaderboard(season: int) -> Optional[dict]:
    r = get_with_retry(URL, params={
        "year": season,
        "type": "pitcher",
        "min": "50",           # min plate appearances against
        "selections": "k_percent,whiff_percent,csw_percent",
        "csv": "true",
    }, timeout=30)
    if r is None:
        return None
    reader = csv.DictReader(io.StringIO(r.text))
    out: dict[str, dict] = {}
    for row in reader:
        pid = row.get("player_id") or row.get("id")
        if not pid:
            continue
        def _f(key: str) -> Optional[float]:
            try:
                return float(row[key])
            except (KeyError, TypeError, ValueError):
                return None
        out[str(pid)] = {
            "k_percent": _f("k_percent"),
            "whiff_percent": _f("whiff_percent"),
            "csw_percent": _f("csw_percent"),
        }
    return out or None


def pitcher_csw(pid: int, season: Optional[int] = None) -> tuple[Optional[dict], bool]:
    """(row, fresh) for one pitcher; row is None when Savant is unreachable
    and no cache exists."""
    season = season or date.today().year
    board, fresh = fetch_cached(f"savant_{season}", lambda: _fetch_leaderboard(season))
    if not board:
        return None, fresh
    return board.get(str(pid)), fresh


def csw_supports_k(row: Optional[dict]) -> Optional[bool]:
    """True when CSW% backs up the K% (not a regression candidate).

    Rule of thumb: K% ≈ 2×CSW% − 32 tracks the league relationship; we call it
    aligned when the pitcher's K% doesn't exceed that implied level by more
    than 3 points. None = no data.
    """
    if not row or row.get("csw_percent") is None or row.get("k_percent") is None:
        return None
    implied_k = 2.0 * row["csw_percent"] - 32.0
    return row["k_percent"] <= implied_k + 3.0
