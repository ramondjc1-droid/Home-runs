# MLB K Analyst ⚾ (+ Home Run Picks)

Automated daily MLB strikeout-prop analyst. Every morning in season it
projects strikeout totals for all probable starters, compares them to
sportsbook K prop lines, and delivers the top 4 highest-edge picks to
**Telegram** at 10 AM ET — with a home-run pick module riding along. Picks
are graded overnight against official box scores and the running record
(units P&L) leads the next morning's card.

> ⚠️ **Informational only — not betting advice.** Projections are
> model-generated screens. Bet responsibly and within your means.

---

## How a pick is made

```
K%_blended    = 0.7 × last-30-day K% + 0.3 × season K%
Adjusted_K    = K%_blended × (Opp team K% L15 ÷ league avg) × ump × park
Projected_Ks  = mean(IP last 5 starts) × 4.3 BF/inning × Adjusted_K
Edge          = Projected_Ks − book line
```

A pick needs **edge ≥ 0.7 Ks** and **confidence ≥ 6/10** (additive factors:
edge size, ≥5 starts, domed park, confirmed high-K ump, CSW% alignment,
line movement, data freshness). Top 4 by confidence; fewer if fewer qualify.
Weights live in [`config/formula.yaml`](config/formula.yaml) and are tunable
live via the `/tune` Telegram command.

The **home run module** models `P(HR) = 1 − (1 − adjusted HR/PA)^4.1` and
takes the top 2 batters whose model probability beats the book's implied
probability by ≥ 3 points.

## Data sources (free tier friendly)

| Source | Role | Key |
|---|---|---|
| MLB Stats API | slate, probables, all K%/IP inputs, box scores | none |
| The Odds API | K + HR prop lines, line movement | `ODDS_API_KEY` |
| WeatherAPI | rain risk + HR temp factor at outdoor parks | `WEATHER_API_KEY` |
| Baseball Savant | CSW% regression check | none |
| FanGraphs | K% cross-check | none |

Every source degrades gracefully: retries → yesterday's cache (−1 confidence,
flagged on the card) → neutral default.

## Quick start (local)

```bash
pip install -r requirements.txt
cp .env.example .env                        # fill in your keys
python src/morning_analysis.py --dry-run    # preview, nothing saved or sent
python src/morning_analysis.py              # live run
python src/telegram_bot.py chatid           # discover your chat id (setup)
pytest tests/                               # offline test suite
```

The system runs with **zero** keys — no odds means projections without edges
(so no picks), and narratives fall back to templates without Anthropic.

## Scheduled heartbeats

| Time (ET) | Stage | What happens |
|---|---|---|
| 2:00 AM | `grader` | grade picks from box scores, roll up units |
| 10:00 AM | `morning_analysis` | yesterday's grade report, then today's picks |
| 2:00 PM | `refresh_lines` | re-fetch lines, alert on moves ≥ 0.5 |
| hourly (evenings) | `pregame_check` | final line check ~1h before each first pitch |

**GitHub Actions (recommended):** add repo secrets `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`, `ODDS_API_KEY`, `WEATHER_API_KEY` —
[`daily.yml`](.github/workflows/daily.yml) handles the schedule (DST-aware via
[`scripts/dispatch.py`](scripts/dispatch.py); the SQLite DB persists between
runs via the Actions cache). Test immediately: **Actions → Run workflow →
stage `morning`**.

**VPS/cron:** run the same dispatcher —

```cron
0 6,7 * * *           cd /path/to/repo && python scripts/dispatch.py --schedule "0 6,7 * * *"
0 14,15 * * *         cd /path/to/repo && python scripts/dispatch.py --schedule "0 14,15 * * *"
0 18,19 * * *         cd /path/to/repo && python scripts/dispatch.py --schedule "0 18,19 * * *"
0 17,20-23,0-3 * * *  cd /path/to/repo && python scripts/dispatch.py --schedule "0 17,20-23,0-3 * * *"
```

## Talking to the bot

Run `python src/command_handler.py` on a machine that stays on, then text:

`/picks` · `/status` · `/grade` · `/week` · `/month` · `/history Skenes` ·
`/tune thresholds.min_edge 0.8` · `/skip COL` · `/add Paul Skenes` — or ask
free-form questions ("why the Skenes over?") answered from the stored pick
data.

## Cost control

Scripted work is plain Python ($0). Opus writes only the final pick
narratives (~6 short calls/day); Sonnet answers chat. Spend is metered into
SQLite against a **$100/month ceiling** — exceeding the projection alerts you
on Telegram and narratives drop to templates. See `src/costs.py`.

## More

- [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md) — full architecture orientation
- [`MAINTENANCE.md`](MAINTENANCE.md) — fix log + annual constants checklist
