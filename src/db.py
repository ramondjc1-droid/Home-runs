"""All SQLite operations for picks, lines, grading, tuning, and cost tracking."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Iterator, Optional

from config import DB_PATH, today_et

SCHEMA = """
CREATE TABLE IF NOT EXISTS picks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            DATE NOT NULL,
    pick_type       TEXT NOT NULL DEFAULT 'K',   -- 'K' strikeout prop, 'HR' home run prop
    pitcher_name    TEXT NOT NULL,               -- player name (batter for HR picks)
    pitcher_id      INTEGER,
    team            TEXT,
    opponent        TEXT,
    game_pk         INTEGER,
    first_pitch_utc TEXT,
    book_line       REAL,
    best_book_name  TEXT,
    best_book_price INTEGER,
    my_projection   REAL,
    edge            REAL,
    confidence      INTEGER,
    pick_side       TEXT,                        -- 'OVER' or 'UNDER'
    narrative       TEXT,
    result          TEXT DEFAULT 'PENDING',      -- 'HIT', 'MISS', 'PUSH', 'PENDING', 'VOID'
    actual_ks       INTEGER,
    units_pnl       REAL,
    pregame_checked INTEGER DEFAULT 0,
    sent            INTEGER DEFAULT 0,
    metrics_json    TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_picks_date ON picks(date);
CREATE INDEX IF NOT EXISTS idx_picks_player ON picks(pitcher_name);

CREATE TABLE IF NOT EXISTS line_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pick_id         INTEGER,
    line            REAL,
    best_price      INTEGER,
    captured_at     TIMESTAMP,
    FOREIGN KEY (pick_id) REFERENCES picks(id)
);

CREATE TABLE IF NOT EXISTS daily_summary (
    date            DATE PRIMARY KEY,
    picks_made      INTEGER,
    hits            INTEGER,
    misses          INTEGER,
    pushes          INTEGER,
    units_pnl       REAL,
    cumulative_units REAL
);

CREATE TABLE IF NOT EXISTS formula_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    parameter       TEXT,
    old_value       TEXT,
    new_value       TEXT,
    reason          TEXT
);

CREATE TABLE IF NOT EXISTS skips (
    label           TEXT NOT NULL,               -- team abbrev, pitcher name, or gamePk
    skip_date       DATE NOT NULL
);

CREATE TABLE IF NOT EXISTS force_adds (
    player_name     TEXT NOT NULL,
    add_date        DATE NOT NULL
);

CREATE TABLE IF NOT EXISTS run_log (
    stage           TEXT NOT NULL,
    run_date        DATE NOT NULL,
    created_at      TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS api_costs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TIMESTAMP NOT NULL,
    model           TEXT NOT NULL,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    usd             REAL
);

CREATE TABLE IF NOT EXISTS kv (
    key             TEXT PRIMARY KEY,
    value           TEXT
);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


# --- picks -------------------------------------------------------------------

def insert_pick(p: dict) -> int:
    cols = ("date", "pick_type", "pitcher_name", "pitcher_id", "team", "opponent",
            "game_pk", "first_pitch_utc", "book_line", "best_book_name",
            "best_book_price", "my_projection", "edge", "confidence", "pick_side",
            "narrative", "metrics_json")
    with connect() as conn:
        cur = conn.execute(
            f"INSERT INTO picks ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            tuple(p.get(c) for c in cols),
        )
        return cur.lastrowid


def mark_sent(pick_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE picks SET sent = 1 WHERE id = ?", (pick_id,))


def picks_for_date(d: Optional[str] = None) -> list[sqlite3.Row]:
    d = d or today_et().isoformat()
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM picks WHERE date = ? ORDER BY confidence DESC, ABS(edge) DESC",
            (d,),
        ).fetchall()


def picks_for_player(name: str) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM picks WHERE pitcher_name LIKE ? ORDER BY date DESC",
            (f"%{name}%",),
        ).fetchall()


def pending_picks(before_date: Optional[str] = None) -> list[sqlite3.Row]:
    """Picks still awaiting a result, optionally limited to dates < before_date."""
    q = "SELECT * FROM picks WHERE result = 'PENDING'"
    args: tuple = ()
    if before_date:
        q += " AND date < ?"
        args = (before_date,)
    with connect() as conn:
        return conn.execute(q + " ORDER BY date", args).fetchall()


def set_result(pick_id: int, result: str, actual: Optional[int],
               units_pnl: Optional[float]) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE picks SET result = ?, actual_ks = ?, units_pnl = ? WHERE id = ?",
            (result, actual, units_pnl, pick_id),
        )


def mark_pregame_checked(pick_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE picks SET pregame_checked = 1 WHERE id = ?", (pick_id,))


# --- line history --------------------------------------------------------------

def record_line(pick_id: int, line: float, best_price: Optional[int] = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO line_history (pick_id, line, best_price, captured_at) "
            "VALUES (?,?,?,?)",
            (pick_id, line, best_price, datetime.utcnow().isoformat()),
        )


def latest_line(pick_id: int) -> Optional[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM line_history WHERE pick_id = ? ORDER BY captured_at DESC LIMIT 1",
            (pick_id,),
        ).fetchone()


# --- daily summary / performance ------------------------------------------------

def upsert_daily_summary(d: str, picks_made: int, hits: int, misses: int,
                         pushes: int, units: float) -> None:
    with connect() as conn:
        prev = conn.execute(
            "SELECT cumulative_units FROM daily_summary WHERE date < ? "
            "ORDER BY date DESC LIMIT 1", (d,)
        ).fetchone()
        cum = (prev["cumulative_units"] if prev else 0.0) + units
        conn.execute(
            "INSERT INTO daily_summary (date, picks_made, hits, misses, pushes, "
            "units_pnl, cumulative_units) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET picks_made=?, hits=?, misses=?, "
            "pushes=?, units_pnl=?, cumulative_units=?",
            (d, picks_made, hits, misses, pushes, units, cum,
             picks_made, hits, misses, pushes, units, cum),
        )


def summary_for(d: str) -> Optional[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM daily_summary WHERE date = ?", (d,)
        ).fetchone()


def performance_since(since_iso: str) -> dict:
    with connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) n,
                      SUM(result = 'HIT') hits,
                      SUM(result = 'MISS') misses,
                      SUM(result = 'PUSH') pushes,
                      COALESCE(SUM(units_pnl), 0) units
               FROM picks WHERE date >= ? AND result != 'PENDING'""",
            (since_iso,),
        ).fetchone()
    return {k: row[k] or 0 for k in row.keys()}


def season_performance() -> dict:
    year = today_et().year
    return performance_since(f"{year}-01-01")


# --- tuning / skips / adds ------------------------------------------------------

def log_formula_change(parameter: str, old: str, new: str, reason: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO formula_changes (changed_at, parameter, old_value, "
            "new_value, reason) VALUES (?,?,?,?,?)",
            (datetime.utcnow().isoformat(), parameter, old, new, reason),
        )


def add_skip(label: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO skips (label, skip_date) VALUES (?, ?)",
                     (label.upper(), today_et().isoformat()))


def skips_for_today() -> set[str]:
    with connect() as conn:
        rows = conn.execute("SELECT label FROM skips WHERE skip_date = ?",
                            (today_et().isoformat(),)).fetchall()
    return {r["label"] for r in rows}


def add_force(player: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO force_adds (player_name, add_date) VALUES (?, ?)",
                     (player, today_et().isoformat()))


def force_adds_for_today() -> set[str]:
    with connect() as conn:
        rows = conn.execute("SELECT player_name FROM force_adds WHERE add_date = ?",
                            (today_et().isoformat(),)).fetchall()
    return {r["player_name"] for r in rows}


# --- run log (idempotency) ------------------------------------------------------

def already_ran_today(stage: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM run_log WHERE stage = ? AND run_date = ? LIMIT 1",
            (stage, today_et().isoformat()),
        ).fetchone()
    return row is not None


def log_run(stage: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO run_log (stage, run_date, created_at) VALUES (?,?,?)",
            (stage, today_et().isoformat(), datetime.utcnow().isoformat()),
        )


def last_run(stage: str) -> Optional[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM run_log WHERE stage = ? ORDER BY created_at DESC LIMIT 1",
            (stage,),
        ).fetchone()


# --- API cost tracking ----------------------------------------------------------

def record_cost(model: str, input_tokens: int, output_tokens: int, usd: float) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO api_costs (ts, model, input_tokens, output_tokens, usd) "
            "VALUES (?,?,?,?,?)",
            (datetime.utcnow().isoformat(), model, input_tokens, output_tokens, usd),
        )


def month_spend() -> float:
    start = today_et().replace(day=1).isoformat()
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(usd), 0) s FROM api_costs WHERE ts >= ?", (start,)
        ).fetchone()
    return row["s"]


# --- misc kv (odds quota, queued reports…) ---------------------------------------

def kv_set(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=?",
            (key, value, value),
        )


def kv_get(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


if __name__ == "__main__":
    init_db()
    print(f"Initialized database at {DB_PATH}")
