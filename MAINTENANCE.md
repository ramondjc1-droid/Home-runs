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
