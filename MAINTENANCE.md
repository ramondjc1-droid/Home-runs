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

### 2026-07-07 — initial build
System created from the earnings-edge-analyst architecture. All data flows
through the official MLB Stats API as the primary source; Baseball Savant
(CSW%) and FanGraphs (K% cross-check) are best-effort enrichments with
day-cache fallbacks; every fallback subtracts 1 confidence point and is
flagged on the pick card.
