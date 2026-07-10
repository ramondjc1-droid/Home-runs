"""MAIN daily script — 10:00 AM ET.

Fetches today's slate, projects strikeouts for every probable starter,
compares against book K-prop lines, scores confidence, selects the top picks
(plus home-run picks), generates Opus narratives, persists everything, and
posts yesterday's grade report followed by today's pick card to Telegram.

    python src/morning_analysis.py --dry-run   # preview, no DB writes / sends
    python src/morning_analysis.py             # live
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime

import cards
import db
import narratives
from config import formula, today_et
from fetchers import fetch_cached, log_error
from fetchers import mlb_stats_api as mlb
from fetchers import odds_api, savant, umpscorecards, weather
from model import confidence, scoring
from telegram_bot import send_message

RUN_DEADLINE_HOUR_ET = (10, 15)  # alert if the run finishes past 10:15 ET


def _park_info(venue: str) -> dict:
    parks = formula().get("parks", {})
    if venue in parks:
        return parks[venue]
    # Fuzzy fallback for renamed venues.
    for name, info in parks.items():
        if name.lower() in venue.lower() or venue.lower() in name.lower():
            return info
    return {"k_factor": 1.0, "hr_factor": 1.0, "dome": False}


def _skipped(game: mlb.Game, skips: set[str]) -> bool:
    labels = {str(game.game_pk), game.home_team.upper(), game.away_team.upper()}
    for p in (game.home_pitcher, game.away_pitcher):
        if p:
            labels.add(p["name"].upper())
    return bool(labels & skips)


def analyze_slate(verbose: bool = True) -> tuple[list, list, list[str]]:
    """Returns (k_projections, hr_projections, flags)."""
    f = formula()
    thr = f["thresholds"]
    flags: list[str] = []
    skips = db.skips_for_today()
    forced = {odds_api.norm_name(n) for n in db.force_adds_for_today()}

    slate, slate_fresh = fetch_cached("slate", lambda: [
        g.__dict__ for g in mlb.todays_slate()])
    if not slate:
        return [], [], ["MLB schedule unreachable"]
    if not slate_fresh:
        flags.append("MLB schedule stale — cached slate used")
    games = [mlb.Game(**g) for g in slate]
    games = [g for g in games if not _skipped(g, skips)]
    if verbose:
        print(f"[scan] {len(games)} games on the slate")

    # Prop lines (one Odds API sweep for the whole slate).
    k_lines = {}
    if odds_api.configured():
        k_lines, lines_fresh = fetch_cached(
            "k_lines_morning", lambda: odds_api.lines_for_slate(odds_api.K_MARKET))
        k_lines = k_lines or {}
        if not lines_fresh and k_lines:
            flags.append("Odds API stale — cached lines used")
    else:
        flags.append("ODDS_API_KEY not set — projections shown without edges")

    # Opponent team K% cache (two lookups per game max).
    team_k: dict[int, float | None] = {}

    def opp_k(team_id: int) -> float | None:
        if team_id not in team_k:
            val, fresh = fetch_cached(f"team_k_{team_id}",
                                      lambda: mlb.team_k_pct_recent(team_id))
            if not fresh and val is not None:
                flags.append("team K% stale for one club")
            team_k[team_id] = val
        return team_k[team_id]

    k_projs = []
    for g in games:
        park = _park_info(g.venue)
        ump_name, ump_factor = umpscorecards.game_ump_k_factor(g.game_pk)
        wx = None
        if not park.get("dome") and weather.configured() and "lat" in park:
            wx = weather.game_time_forecast(park["lat"], park["lon"], g.game_date_utc)

        for pitcher, team, opp_team_id, opp_abbr in (
            (g.home_pitcher, g.home_team, g.away_team_id, g.away_team),
            (g.away_pitcher, g.away_team, g.home_team_id, g.home_team),
        ):
            if not pitcher:
                continue
            stale = 0
            profile, fresh = fetch_cached(
                f"pitcher_{pitcher['id']}",
                lambda pid=pitcher["id"]: mlb.pitcher_k_profile(pid))
            if profile is None:
                flags.append(f"{pitcher['name']}: no stats — skipped")
                continue
            if not fresh:
                stale += 1
                flags.append(f"{pitcher['name']}: stats stale — fallback used")
            if (profile["starts"] < f["formula"]["min_starts_required"]
                    and odds_api.norm_name(pitcher["name"]) not in forced):
                continue

            opp_k_pct = opp_k(opp_team_id)
            proj = scoring.project_strikeouts(
                pitcher_name=pitcher["name"], pitcher_id=pitcher["id"],
                team=team, opponent=opp_abbr, game_pk=g.game_pk,
                k_pct_30d=profile["k_pct_30d"],
                k_pct_season=profile["k_pct_season"],
                ip_last5_mean=profile["ip_last5_mean"],
                starts=profile["starts"],
                opp_k_pct_l15=opp_k_pct,
                ump_k_factor=ump_factor, park_k_factor=park.get("k_factor", 1.0),
            )
            proj.extras.update({
                "first_pitch_utc": g.game_date_utc, "venue": g.venue,
                "dome": bool(park.get("dome")), "ump": ump_name,
                "hr_per_bf": profile.get("hr_per_bf"),
                "weather": wx, "profile": profile,
            })
            if wx and wx["precip_chance"] >= 50:
                proj.flags.append(f"{wx['precip_chance']}% rain risk")

            # Attach book line + edge.
            rec = k_lines.get(odds_api.norm_name(pitcher["name"]))
            if rec and rec.get("line") is not None:
                scoring.apply_line(proj, rec["line"])
                book, price = odds_api.best_price_for(rec, proj.side)
                proj.extras.update({"best_book": book, "best_price": price})

            # CSW% regression check (Savant enrichment).
            csw_row, _ = savant.pitcher_csw(pitcher["id"])
            csw_aligned = savant.csw_supports_k(csw_row)

            confidence.score_k_pick(
                proj, domed_park=bool(park.get("dome")),
                ump_confirmed=ump_name is not None,
                ump_high_k=umpscorecards.is_high_k_ump(ump_factor),
                injury_flags=False, csw_aligned=csw_aligned,
                line_move=None, stale_sources=stale,
            )
            k_projs.append(proj)

    # Home run module.
    hr_projs = []
    hr_cfg = f.get("homerun", {})
    if hr_cfg.get("enabled", True):
        hr_lines = {}
        if odds_api.configured():
            hr_lines, _ = fetch_cached(
                "hr_lines_morning",
                lambda: odds_api.lines_for_slate(odds_api.HR_MARKET))
            hr_lines = hr_lines or {}
        for g in games:
            park = _park_info(g.venue)
            wx = None
            if not park.get("dome") and weather.configured() and "lat" in park:
                wx = weather.game_time_forecast(park["lat"], park["lon"],
                                                g.game_date_utc)
            for team_id, team, opp_abbr, opp_pitcher in (
                (g.home_team_id, g.home_team, g.away_team, g.away_pitcher),
                (g.away_team_id, g.away_team, g.home_team, g.home_pitcher),
            ):
                leaders, _ = fetch_cached(
                    f"hr_leaders_{team_id}",
                    lambda tid=team_id: mlb.team_hr_leaders(
                        tid, limit=hr_cfg.get("candidates_per_team", 5)))
                pitcher_hr = None
                if opp_pitcher:
                    prof, _ = fetch_cached(
                        f"pitcher_{opp_pitcher['id']}",
                        lambda pid=opp_pitcher["id"]: mlb.pitcher_k_profile(pid))
                    pitcher_hr = (prof or {}).get("hr_per_bf")
                for batter in (leaders or []):
                    bprof, _ = fetch_cached(
                        f"batter_{batter['id']}",
                        lambda bid=batter["id"]: mlb.batter_hr_profile(bid))
                    if not bprof or bprof["pa_season"] < hr_cfg.get("min_pa_season", 100):
                        continue
                    hp = scoring.project_home_run(
                        batter_name=batter["name"], batter_id=batter["id"],
                        team=team, opponent=opp_abbr, game_pk=g.game_pk,
                        hr_pa_30d=bprof["hr_pa_30d"],
                        hr_pa_season=bprof["hr_pa_season"],
                        park_hr_factor=park.get("hr_factor", 1.0),
                        pitcher_hr_per_bf=pitcher_hr,
                        temp_f=wx["temp_f"] if wx else None,
                    )
                    hp.extras["first_pitch_utc"] = g.game_date_utc
                    rec = hr_lines.get(odds_api.norm_name(batter["name"]))
                    if rec:
                        _, price = odds_api.best_price_for(rec, "OVER")
                        if price:
                            scoring.apply_hr_price(hp, price)
                            hp.extras["best_book"] = odds_api.best_price_for(rec, "OVER")[0]
                    # An "edge" this big is a stale/in-play price, not value.
                    if (hp.edge or 0) > hr_cfg.get("max_plausible_edge", 0.15):
                        flags.append(f"{batter['name']}: implausible HR edge "
                                     f"({hp.edge:+.2f}) — price discarded")
                        hp.edge = hp.implied_prob = hp.book_price = None
                    confidence.score_hr_pick(
                        hp, domed_park=bool(park.get("dome")),
                        injury_flags=False, pa_season=bprof["pa_season"])
                    hr_projs.append(hp)

    # Moneyline module: team win probability vs best h2h price.
    ml_projs = []
    ml_cfg = f.get("moneyline", {})
    if ml_cfg.get("enabled", True) and odds_api.configured():
        recs, recs_fresh = fetch_cached("standings", mlb.standings)
        ml_odds, _ = fetch_cached("ml_lines_morning", odds_api.moneylines_for_slate)
        if recs and ml_odds:
            if not recs_fresh:
                flags.append("standings stale — cached records used")
            by_names = {(e["home_team"].lower(), e["away_team"].lower()): e
                        for e in ml_odds if e.get("home_team") and e.get("away_team")}
            for g in games:
                home_rec, away_rec = recs.get(str(g.home_team_id)) or recs.get(g.home_team_id), \
                                     recs.get(str(g.away_team_id)) or recs.get(g.away_team_id)
                if not home_rec or not away_rec:
                    continue

                def _kbb(p):
                    if not p:
                        return None
                    prof, _ = fetch_cached(f"pitcher_{p['id']}",
                                           lambda pid=p["id"]: mlb.pitcher_k_profile(pid))
                    return (prof or {}).get("kbb_pct")

                home_kbb, away_kbb = _kbb(g.home_pitcher), _kbb(g.away_pitcher)
                prob_home, breakdown = scoring.project_home_win_prob(
                    home_rec=home_rec, away_rec=away_rec,
                    home_kbb=home_kbb, away_kbb=away_kbb)

                ev = by_names.get((g.home_team_name.lower(), g.away_team_name.lower()))
                if not ev:
                    continue
                park = _park_info(g.venue)
                sample = min(home_rec["w"] + home_rec["l"],
                             away_rec["w"] + away_rec["l"])
                both_named = bool(g.home_pitcher and g.away_pitcher)
                for team_abbr, team_id, team_name, opp_abbr, prob, is_home in (
                    (g.home_team, g.home_team_id, g.home_team_name,
                     g.away_team, prob_home, True),
                    (g.away_team, g.away_team_id, g.away_team_name,
                     g.home_team, 1.0 - prob_home, False),
                ):
                    priced = ev["best"].get(team_name)
                    if not priced:
                        continue
                    book, price = priced
                    proj = scoring.MLProjection(
                        team=team_abbr, team_id=team_id, opponent=opp_abbr,
                        game_pk=g.game_pk, win_prob=round(prob, 4), is_home=is_home)
                    scoring.apply_ml_price(proj, price)
                    # An "edge" this big is a stale/in-play price, not value.
                    if (proj.edge or 0) > ml_cfg.get("max_plausible_edge", 0.20):
                        flags.append(f"{team_abbr} ML: implausible edge "
                                     f"({proj.edge:+.2f}) — price discarded")
                        continue
                    proj.extras.update({"best_book": book, "breakdown": breakdown,
                                        "first_pitch_utc": g.game_date_utc,
                                        "team_name": team_name})
                    confidence.score_ml_pick(
                        proj, both_starters_named=both_named,
                        domed_park=bool(park.get("dome")), sample_games=sample)
                    ml_projs.append(proj)

    # Totals module: expected runs vs the book's O/U line. Reuses the same
    # standings + odds sweep the moneyline module already fetched.
    tot_projs = []
    tot_cfg = f.get("totals", {})
    if (tot_cfg.get("enabled", True) and odds_api.configured()
            and ml_cfg.get("enabled", True)):
        recs, _ = fetch_cached("standings", mlb.standings)
        ml_odds, _ = fetch_cached("ml_lines_morning", odds_api.moneylines_for_slate)
        if recs and ml_odds:
            by_names = {(e["home_team"].lower(), e["away_team"].lower()): e
                        for e in ml_odds if e.get("home_team") and e.get("away_team")}
            for g in games:
                home_rec = recs.get(str(g.home_team_id)) or recs.get(g.home_team_id)
                away_rec = recs.get(str(g.away_team_id)) or recs.get(g.away_team_id)
                ev = by_names.get((g.home_team_name.lower(), g.away_team_name.lower()))
                if not home_rec or not away_rec or not ev or not ev.get("total"):
                    continue
                tot = ev["total"]
                park = _park_info(g.venue)
                wx = None
                if not park.get("dome") and weather.configured() and "lat" in park:
                    wx = weather.game_time_forecast(park["lat"], park["lon"],
                                                    g.game_date_utc)

                def _kbb(p):
                    if not p:
                        return None
                    prof, _ = fetch_cached(f"pitcher_{p['id']}",
                                           lambda pid=p["id"]: mlb.pitcher_k_profile(pid))
                    return (prof or {}).get("kbb_pct")

                runs, breakdown = scoring.project_total_runs(
                    home_rec=home_rec, away_rec=away_rec,
                    home_kbb=_kbb(g.home_pitcher), away_kbb=_kbb(g.away_pitcher),
                    park_run_factor=park.get("run_factor", 1.0),
                    temp_f=wx["temp_f"] if wx else None)

                proj = scoring.TotalProjection(
                    home_team=g.home_team, away_team=g.away_team,
                    game_pk=g.game_pk, projected_runs=round(runs, 2))
                over = tot.get("over") or [None, None]
                under = tot.get("under") or [None, None]
                scoring.apply_total_line(proj, tot["line"], over[1], under[1])
                if abs(proj.edge or 0) > tot_cfg.get("max_plausible_edge_runs", 3.0):
                    flags.append(f"{proj.matchup}: implausible totals edge "
                                 f"({proj.edge:+.1f}) — discarded")
                    continue
                proj.extras.update({
                    "breakdown": breakdown,
                    "best_book": (over[0] if proj.side == "OVER" else under[0]),
                    "first_pitch_utc": g.game_date_utc,
                })
                rain = bool(wx and wx["precip_chance"] >= 50)
                if rain:
                    proj.flags.append(f"{proj.matchup}: {wx['precip_chance']}% rain risk")
                sample = min(home_rec["w"] + home_rec["l"],
                             away_rec["w"] + away_rec["l"])
                confidence.score_total_pick(
                    proj, both_starters_named=bool(g.home_pitcher and g.away_pitcher),
                    domed_park=bool(park.get("dome")), sample_games=sample,
                    rain_risk=rain)
                tot_projs.append(proj)

    return k_projs, hr_projs, ml_projs, tot_projs, flags


def select_picks(k_projs: list, hr_projs: list, ml_projs: list,
                 tot_projs: list) -> tuple[list, list, list, list]:
    f = formula()
    thr = f["thresholds"]
    qualified = [p for p in k_projs
                 if p.edge is not None
                 and abs(p.edge) >= thr["min_edge"]
                 and p.confidence >= thr["min_confidence"]]
    qualified.sort(key=lambda p: (p.confidence, abs(p.edge or 0)), reverse=True)
    k_picks = qualified[:thr["max_picks_per_day"]]

    hr_cfg = f.get("homerun", {})
    hr_q = [p for p in hr_projs
            if p.edge is not None
            and p.edge >= hr_cfg.get("min_edge_prob", 0.03)
            and p.confidence >= hr_cfg.get("min_confidence", 6)]
    hr_q.sort(key=lambda p: (p.confidence, p.edge or 0), reverse=True)
    # One HR pick per batter (books list dupes across events occasionally).
    seen, hr_picks = set(), []
    for p in hr_q:
        if p.batter_id in seen:
            continue
        seen.add(p.batter_id)
        hr_picks.append(p)
        if len(hr_picks) >= hr_cfg.get("max_picks_per_day", 2):
            break

    ml_cfg = f.get("moneyline", {})
    ml_q = [p for p in ml_projs
            if p.edge is not None
            and p.edge >= ml_cfg.get("min_edge_prob", 0.04)
            and p.confidence >= ml_cfg.get("min_confidence", 6)]
    ml_q.sort(key=lambda p: (p.confidence, p.edge or 0), reverse=True)
    seen_games, ml_picks = set(), []   # never both sides of one game
    for p in ml_q:
        if p.game_pk in seen_games:
            continue
        seen_games.add(p.game_pk)
        ml_picks.append(p)
        if len(ml_picks) >= ml_cfg.get("max_picks_per_day", 2):
            break

    tot_cfg = f.get("totals", {})
    tot_q = [p for p in tot_projs
             if p.edge is not None
             and abs(p.edge) >= tot_cfg.get("min_edge_runs", 0.75)
             and p.confidence >= tot_cfg.get("min_confidence", 6)]
    tot_q.sort(key=lambda p: (p.confidence, abs(p.edge or 0)), reverse=True)
    tot_picks = tot_q[:tot_cfg.get("max_picks_per_day", 2)]
    return k_picks, hr_picks, ml_picks, tot_picks


def _persist_k(p) -> int:
    pick_id = db.insert_pick({
        "date": today_et().isoformat(), "pick_type": "K",
        "pitcher_name": p.pitcher_name, "pitcher_id": p.pitcher_id,
        "team": p.team, "opponent": p.opponent, "game_pk": p.game_pk,
        "first_pitch_utc": p.extras.get("first_pitch_utc"),
        "book_line": p.book_line, "best_book_name": p.extras.get("best_book"),
        "best_book_price": p.extras.get("best_price"),
        "my_projection": p.projected_ks, "edge": p.edge,
        "confidence": p.confidence, "pick_side": p.side,
        "narrative": p.extras.get("narrative"),
        "metrics_json": json.dumps({
            "k_pct_blended": p.k_pct_blended, "opp_factor": p.opp_factor,
            "ump_factor": p.ump_factor, "park_factor": p.park_factor,
            "bf_expected": p.bf_expected, "conf_reasons": p.conf_reasons,
            "ump": p.extras.get("ump"), "venue": p.extras.get("venue"),
        }, default=str),
    })
    if p.book_line is not None:
        db.record_line(pick_id, p.book_line, p.extras.get("best_price"))
    return pick_id


def _persist_hr(p) -> int:
    pick_id = db.insert_pick({
        "date": today_et().isoformat(), "pick_type": "HR",
        "pitcher_name": p.batter_name, "pitcher_id": p.batter_id,
        "team": p.team, "opponent": p.opponent, "game_pk": p.game_pk,
        "first_pitch_utc": p.extras.get("first_pitch_utc"),
        "book_line": 0.5, "best_book_name": p.extras.get("best_book"),
        "best_book_price": p.book_price,
        "my_projection": p.hr_prob, "edge": p.edge,
        "confidence": p.confidence, "pick_side": "OVER",
        "narrative": p.extras.get("narrative"),
        "metrics_json": json.dumps(
            {"hr_pa_adj": p.hr_pa_adj, "implied": p.implied_prob,
             "conf_reasons": p.conf_reasons, **p.extras}, default=str),
    })
    return pick_id


def _persist_ml(p) -> int:
    pick_id = db.insert_pick({
        "date": today_et().isoformat(), "pick_type": "ML",
        "pitcher_name": p.extras.get("team_name", p.team), "pitcher_id": p.team_id,
        "team": p.team, "opponent": p.opponent, "game_pk": p.game_pk,
        "first_pitch_utc": p.extras.get("first_pitch_utc"),
        "book_line": p.implied_prob, "best_book_name": p.extras.get("best_book"),
        "best_book_price": p.book_price,
        "my_projection": p.win_prob, "edge": p.edge,
        "confidence": p.confidence, "pick_side": "ML",
        "narrative": p.extras.get("narrative"),
        "metrics_json": json.dumps(
            {"conf_reasons": p.conf_reasons, **p.extras}, default=str),
    })
    return pick_id


def _persist_tot(p) -> int:
    pick_id = db.insert_pick({
        "date": today_et().isoformat(), "pick_type": "TOT",
        "pitcher_name": p.matchup, "pitcher_id": None,
        "team": p.home_team, "opponent": p.away_team, "game_pk": p.game_pk,
        "first_pitch_utc": p.extras.get("first_pitch_utc"),
        "book_line": p.book_line, "best_book_name": p.extras.get("best_book"),
        "best_book_price": p.book_price,
        "my_projection": p.projected_runs, "edge": p.edge,
        "confidence": p.confidence, "pick_side": p.side,
        "narrative": p.extras.get("narrative"),
        "metrics_json": json.dumps(
            {"conf_reasons": p.conf_reasons, **p.extras}, default=str),
    })
    if p.book_line is not None:
        db.record_line(pick_id, p.book_line, p.book_price)
    return pick_id


def run(dry_run: bool = False, verbose: bool = True) -> list:
    db.init_db()
    started = datetime.now()

    # Self-heal: if the overnight grader was delayed or dropped by the
    # scheduler, grade yesterday's pending picks now so the card always
    # leads with real results.
    if not dry_run:
        try:
            import grader
            grader.run(notify=False)
        except Exception as exc:
            log_error("morning_analysis", f"pre-grade failed: {exc}")

    k_projs, hr_projs, ml_projs, tot_projs, flags = analyze_slate(verbose=verbose)
    if not k_projs and not hr_projs and not ml_projs and not tot_projs:
        body = cards.no_slate() if not flags else (
            "⚠️ <b>Morning analysis failed</b> — " + "; ".join(flags))
        print(body) if dry_run else send_message(body)
        return []

    k_picks, hr_picks, ml_picks, tot_picks = select_picks(
        k_projs, hr_projs, ml_projs, tot_projs)
    if verbose:
        print(f"[scan] {len(k_projs)} pitchers projected, "
              f"{len(k_picks)} K + {len(hr_picks)} HR + {len(ml_picks)} ML "
              f"+ {len(tot_picks)} TOT picks qualify")

    # Narratives (Opus, budget-gated) + card blocks.
    k_blocks, hr_blocks, ml_blocks, tot_blocks = [], [], [], []
    for i, p in enumerate(k_picks):
        p.extras["narrative"] = narratives.generate(p)
        k_blocks.append(cards.k_pick_block(i, p, p.extras["narrative"]))
        flags.extend(p.flags)
    for i, p in enumerate(hr_picks):
        p.extras["narrative"] = narratives.generate(p)
        hr_blocks.append(cards.hr_pick_block(i, p, p.extras["narrative"]))
    for i, p in enumerate(ml_picks):
        p.extras["narrative"] = narratives.generate(p)
        ml_blocks.append(cards.ml_pick_block(i, p, p.extras["narrative"]))
    for i, p in enumerate(tot_picks):
        p.extras["narrative"] = narratives.generate(p)
        tot_blocks.append(cards.tot_pick_block(i, p, p.extras["narrative"]))
        flags.extend(p.flags)

    board = None
    if formula().get("moneyline", {}).get("show_board", True):
        board = cards.ml_board(ml_projs, {p.game_pk for p in ml_picks})

    grade = cards.grade_report()
    card = cards.morning_card(k_blocks, hr_blocks, sorted(set(flags)),
                              ml_blocks=ml_blocks, ml_board_text=board,
                              tot_blocks=tot_blocks)

    if dry_run:
        print("\n" + "=" * 60)
        if grade:
            print(grade + "\n")
        print(card)
        print("=" * 60)
        print("\n[dry-run] nothing saved or sent. Re-run without --dry-run to go live.")
        return k_picks + hr_picks + ml_picks + tot_picks

    ids = ([_persist_k(p) for p in k_picks]
           + [_persist_hr(p) for p in hr_picks]
           + [_persist_ml(p) for p in ml_picks]
           + [_persist_tot(p) for p in tot_picks])
    if board:  # so /picks can re-show the board later in the day
        db.kv_set("ml_board", json.dumps(
            {"date": today_et().isoformat(), "text": board}))
    ok = True
    if grade:
        ok = send_message(grade)
    ok = send_message(card) and ok
    if ok:
        for pid in ids:
            db.mark_sent(pid)

    elapsed = (datetime.now() - started).total_seconds()
    if verbose:
        print(f"[scan] done in {elapsed:.0f}s — {len(ids)} picks saved.")
    return k_picks + hr_picks + ml_picks + tot_picks


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="MLB K Analyst — morning analysis")
    ap.add_argument("--dry-run", action="store_true",
                    help="preview picks without saving or sending")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    try:
        run(dry_run=args.dry_run, verbose=not args.quiet)
    except Exception as exc:
        log_error("morning_analysis", f"fatal: {exc}")
        if not args.dry_run:
            send_message(f"🚨 <b>Morning analysis crashed</b>: <code>{exc}</code>\n"
                         "Check logs/errors.log.")
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
