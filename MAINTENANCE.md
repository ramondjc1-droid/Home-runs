# Maintenance log

Running log of fixes, scraper repairs, and annual constants that need
refreshing. Newest entries first.

## Annual checklist (each March, before Opening Day)
- [ ] Update `formula.league_avg_k_pct` in `config/formula.yaml` to last season's MLB average K%.
- [ ] Update `homerun.league_avg_hr_pa` the same way.
- [ ] Review `parks:` — venue renames (e.g. Minute Maid → Daikin in 2025), team relocations, new roofs.
- [ ] Refresh `umpire_k_factors:` from the latest UmpScorecards data.
- [ ] Verify The Odds API market keys are still `pitcher_strikeouts` / `batter_home_runs`.

## Log

### 2026-10-01 — quota resets at the month boundary (401s stop, no key change)
Closes the 23-day degraded run that began 09-08. Every morning 09-08 → 09-29
logged `[error] odds_api: ... 401` followed by `[scan] odds down — sent model
projections`; nothing was persisted, so `refresh_lines` had no picks to check
and made zero API calls. On 10-01 the 401s disappeared with **no human action
and no new key** — the third month boundary in a row to clear on its own
(08-01, 09-01, 10-01). This confirms metered-monthly billing rather than a
hard account cap, and confirms the key itself was never invalid.

Quota is billed per ACCOUNT, not per key: issuing a fresh key on an exhausted
account inherits the spent quota and 401s immediately. Only lower request
volume or a plan upgrade shortens the outage; patience ends it on the 1st.

Burn math, for whoever tunes this next: player props are per-event endpoints,
so one pass costs `1 + N` requests, and K and HR are fetched in two separate
passes — every event is fetched twice. A 15-game slate is ~60 requests per
full refresh, times 3 refreshes/day. The module docstring's "~50 requests/day"
estimate is low by roughly 3-4x because it does not account for the second
market pass. Combining both markets into one per-event call, dropping the
duplicate `events()` call, and scoping `refresh_lines` to events that actually
have live picks would cut consumption by about that factor.

Caveat on 10-01 itself: the slate was a single postseason game and no prop
lines came back, so `odds down` still fired and no picks were saved. Zero
errors means every request returned 200 — the API is answering again — but a
multi-game slate with posted props is still needed to show the full path end
to end.

### 2026-07-20 — Odds API 401 → silent empty cards (root cause of "no picks")
The ODDS_API_KEY began returning 401 Unauthorized (invalid/rotated key or
exhausted monthly quota). With no book lines, every projection failed the edge
gate → 0 picks → the morning card shipped effectively empty. Because a Telegram
send failure and a zero-pick day both let the job exit 0, this hid as
"successful delivery." Fix: analyze_slate now emits an ODDS_API_DOWN flag when
games exist but no lines return, and morning_analysis sends a loud 🚨 Telegram
alert instead of a silent card. When this fires, check the-odds-api.com
dashboard for key status and remaining quota — but see the 2026-10-01 entry
before issuing a new key: quota is billed per account, so a fresh key on an
exhausted account 401s immediately. Also a process lesson: verify the [scan]
pick count in logs, never treat job-success as delivery-success.

### 2026-07-13 — All-Star break crash (first empty slate)
The empty-slate early return in analyze_slate still returned the pre-ML/TOT
3-tuple while the caller unpacked 5 values; the first no-games day (All-Star
Monday) crashed the morning run. Fixed the arity, distinguished "no games"
(friendly off-day message) from "schedule unreachable" (error flag), and made
off-days still deliver yesterday's grade report. Regression test added.

### 2026-07-08 — in-play price leakage + UTC date bug (caught in dry run)
An evening dry run produced absurd "edges" (+33 pt HR at +16000, +37 pt ML at
+1150): the UTC runner clock had rolled past midnight (targeting tomorrow's
slate) while The Odds API returned live in-game prices for tonight's games.
Fixes: (1) all slate dates now use today_et() (America/New_York) instead of
date.today(); (2) odds fetchers drop events whose commence_time has passed —
in-play prices never enter the model; (3) sanity gates discard any HR edge
> 0.15 or ML edge > 0.20 as a data error, with a flag on the card.

### 2026-07-07 — HR model calibration before first live run
First live-slate dry run showed HR probability edges of +12 to +14 points —
implausibly large. Tightened the pitcher HR factor clamp from [0.6, 1.6] to
[0.8, 1.25], added 25% shrinkage of blended HR/PA toward league average
(`homerun.shrink_to_league`), and raised `homerun.min_edge_prob` 0.03 → 0.04.
K model untouched. Owner approved going live for the next morning's slate.

### 2026-07-07 — initial build
System created from the earnings-edge-analyst architecture. All data flows
through the official MLB Stats API as the primary source; Baseball Savant
(CSW%) and FanGraphs (K% cross-check) are best-effort enrichments with
day-cache fallbacks; every fallback subtracts 1 confidence point and is
flagged on the pick card.
