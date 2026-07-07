# PROJECT CONTEXT — mlb-k-analyst

Read this first if you (or a future instance of you) need to rebuild, debug,
or extend the system.

## What this is

An automated daily MLB props analyst. In season (late March – October), every
morning it:

1. Pulls today's slate + probable starters (MLB Stats API).
2. Projects each starter's strikeouts with the tunable formula below.
3. Compares projections to sportsbook K prop lines (The Odds API).
4. Scores 1-10 confidence from additive factors (edge size, sample size,
   dome, ump, CSW% alignment, line movement, data staleness).
5. Picks the top 4 (edge ≥ 0.7 AND confidence ≥ 6; fewer if fewer qualify).
6. Also runs a smaller **home run pick** module (model P(HR) vs implied
   probability from "to hit a HR" odds; top 2 by default).
7. Writes 2-sentence Opus narratives, saves everything to SQLite, and posts
   yesterday's grade report + today's pick card to Telegram at 10 AM ET.

Lines are re-checked at 2 PM ET and ~1 hour before each first pitch, with
Telegram alerts on moves ≥ 0.5. At 2 AM ET the grader marks each pick
HIT/MISS/PUSH/VOID from official box scores and rolls up units P&L
(priced at the stored best book price, else -110).

## The math

```
K%_blended    = 0.7 × K%_last30d + 0.3 × K%_season          (weights tunable)
Adjusted_K    = K%_blended × clamp(OppTeamK%_L15 / LeagueK%) × UmpF × ParkF
BF_expected   = mean(IP last 5 starts) × 4.3
Projected_Ks  = BF_expected × Adjusted_K
Edge          = Projected_Ks − Book_Line     → OVER if +, UNDER if −

HR module:
HR/PA_blended = 0.7 × HR/PA_30d + 0.3 × HR/PA_season
HR/PA_adj     = blended × ParkHRF × PitcherHRF × TempF
P(HR)         = 1 − (1 − HR/PA_adj)^4.1
Edge          = P(HR) − implied_prob(best Yes price)
```

All weights, thresholds, park factors, and ump factors live in
`config/formula.yaml` — tunable live via the `/tune` Telegram command; every
change is logged to the `formula_changes` table.

## Data sources & degradation ladder

| Source | Used for | On failure |
|---|---|---|
| MLB Stats API (statsapi.mlb.com, keyless) | slate, probables, K% (season + 30d via game logs), IP/start, opponent team K% (byDateRange ≈ L15), HR rates, HR leaders, HP umpire, box scores | 3 retries w/ backoff → day-cache fallback (−1 confidence, flagged) |
| The Odds API (`ODDS_API_KEY`) | `pitcher_strikeouts` + `batter_home_runs` props, per-event endpoints; quota header stored in kv for `/status` | picks shown without edges; no picks qualify |
| Baseball Savant (custom-leaderboard CSV) | CSW% regression check (`csw_aligns_with_k` confidence point) | point simply doesn't fire |
| FanGraphs (leaders JSON API) | secondary K% cross-check | ignored |
| WeatherAPI (`WEATHER_API_KEY`) | rain-risk flags, HR temp factor (outdoor parks only) | no weather flags |
| UmpScorecards | K factors are *seeded* in config; assignment comes from the MLB game feed (posts ~1-3h pregame, so mornings are usually "unconfirmed") | neutral 1.0 factor |

Design decision: **everything that can come from the official MLB Stats API
does** — scrapers are enrichment only, so a scraper breaking can never kill
the morning run.

## Model strategy (cost control)

- Plain Python (free): fetching, scoring, SQLite, grading, line checks.
- `NARRATIVE_MODEL` (default `claude-opus-4-7`): ONLY the final pick
  narratives — max ~6 short calls/day.
- `CHAT_MODEL` (default `claude-sonnet-4-6`): free-text Telegram Q&A.
- `src/costs.py` logs every call's tokens/$ to the `api_costs` table.
  When projected month spend exceeds `budget.monthly_ceiling_usd` ($100) or
  tracked spend nears the ceiling (within `low_balance_alert_usd`, $20), a
  Telegram alert fires (once/day) and narratives fall back to templates.

## Layout

```
config/formula.yaml      tunable weights + park/ump tables + budget
src/
  morning_analysis.py    MAIN daily script (--dry-run supported)
  refresh_lines.py       2 PM ET line movement check
  pregame_check.py       hourly; final check ~1h before each first pitch
  grader.py              2 AM ET grading from box scores
  command_handler.py     Telegram long-poll: /picks /status /grade /week
                         /month /history /tune /skip /add + NL Q&A (Sonnet)
  cards.py               Telegram HTML card formatting
  narratives.py          Opus narratives (budget-gated, template fallback)
  costs.py               spend tracking + budget alerts
  db.py                  SQLite: picks, line_history, daily_summary,
                         formula_changes, skips, force_adds, run_log,
                         api_costs, kv
  model/scoring.py       projection math   model/confidence.py  1-10 scoring
  fetchers/              mlb_stats_api, odds_api, weather, savant,
                         fangraphs, umpscorecards + retry/cache plumbing
scripts/dispatch.py      cron-string → stage router (idempotent per day;
                         pregame stage exempt, it self-noops)
.github/workflows/       daily.yml (heartbeats, DB persisted via actions
                         cache) + ci.yml (pytest)
tests/test_pipeline.py   offline tests of math/grading/db/dispatch
data/picks.db            SQLite (created on first run; gitignored)
logs/errors.log          error trail (gitignored)
```

## Scheduling

GitHub Actions cron is UTC-only and DST-blind, so `daily.yml` fires each
stage at both candidate UTC hours and `scripts/dispatch.py` routes by which
cron string fired (never wall clock — Actions can lag hours). Per-day
idempotency lives in the `run_log` table. On a VPS, run the same dispatcher
from crontab, or run each script directly at 2:00 / 10:00 / 14:00 ET plus
hourly evenings for `pregame_check.py`.

## Grading edge cases

- Pitcher scratched / never appeared → VOID (0 units), noted on the report.
- Whole-number lines push on exact match; half lines can't push.
- Game not final at 2 AM (west coast extras) → stays PENDING; the grader
  re-checks it the next night and the summary rolls up then.

## Known gaps / future work

- Opponent K% uses a 15-*calendar-day* team hitting window (byDateRange), a
  close proxy for "last 15 games".
- Injury flags are a placeholder (`no_injury_flags` currently always awards
  its point); wire an IL-report fetcher to make it real.
- Ump K factors are config seeds, not live UmpScorecards data.
- HR odds matching assumes The Odds API `batter_home_runs` Yes/No format.
- `/tune` rewrites `config/formula.yaml` via yaml.safe_dump, which strips the
  comments — the tuning history lives in the `formula_changes` table instead.
