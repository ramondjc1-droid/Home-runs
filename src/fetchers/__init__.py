"""Shared fetcher plumbing: retries with backoff, and a JSON day-cache.

Per the error-handling contract:
  1. every network call retries 3x with exponential backoff (2s, 4s, 8s);
  2. on total failure, callers fall back to the most recent cached value and
     the pipeline flags the data as stale (confidence -1);
  3. failures land in logs/errors.log.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from config import CACHE_DIR, LOGS_DIR

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
RETRIES = 3
BACKOFF = 2.0


def log_error(source: str, message: str) -> None:
    line = f"{datetime.utcnow().isoformat()} [{source}] {message}\n"
    try:
        with open(LOGS_DIR / "errors.log", "a") as fh:
            fh.write(line)
    except OSError:
        pass
    print(f"[error] {source}: {message}")


def get_with_retry(url: str, params: Optional[dict] = None, timeout: int = 20,
                   headers: Optional[dict] = None) -> Optional[requests.Response]:
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    last_exc: Exception | None = None
    for attempt in range(RETRIES):
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=hdrs)
            if r.status_code == 429:
                raise RuntimeError("rate limited (429)")
            r.raise_for_status()
            return r
        except Exception as exc:
            last_exc = exc
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF * (2 ** attempt))
    log_error("http", f"GET {url} failed after {RETRIES} tries: {last_exc}")
    return None


# --- JSON day-cache -----------------------------------------------------------

def _cache_path(name: str) -> Path:
    return CACHE_DIR / f"{name}.json"


def cache_set(name: str, value: Any) -> None:
    try:
        with open(_cache_path(name), "w") as fh:
            json.dump({"date": date.today().isoformat(), "value": value}, fh)
    except OSError as exc:
        log_error("cache", f"write {name}: {exc}")


def cache_get(name: str, max_age_days: int = 3) -> tuple[Optional[Any], bool]:
    """Return (value, fresh). fresh=True only when cached today.

    Values older than max_age_days return (None, False).
    """
    path = _cache_path(name)
    if not path.exists():
        return None, False
    try:
        with open(path) as fh:
            blob = json.load(fh)
        cached = date.fromisoformat(blob["date"])
        age = (date.today() - cached).days
        if age > max_age_days:
            return None, False
        return blob["value"], age == 0
    except Exception as exc:
        log_error("cache", f"read {name}: {exc}")
        return None, False


def fetch_cached(name: str, fetch: Callable[[], Any]) -> tuple[Optional[Any], bool]:
    """Fetch fresh data, caching it; on failure serve stale cache.

    Returns (value, fresh). fresh=False means the pick card must flag stale
    data and the confidence model applies its penalty.
    """
    try:
        value = fetch()
    except Exception as exc:
        log_error(name, str(exc))
        value = None
    if value is not None:
        cache_set(name, value)
        return value, True
    stale, _ = cache_get(name)
    return stale, False
