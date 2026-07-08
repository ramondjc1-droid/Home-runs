"""Offline smoke tests — no network, no API keys required."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Route the DB to a temp file before importing db.
import config  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "picks.db")
    db.init_db()
    yield


# --- the math -------------------------------------------------------------------

def test_k_projection_matches_hand_math():
    from model import scoring
    # 70/30 blend of 30% / 25% = 28.5%; opp 22.8% (league avg → factor 1.0);
    # ump 1.0, park 1.0; 6 IP × 4.3 = 25.8 BF; 25.8 × .285 = 7.353
    p = scoring.project_strikeouts(
        pitcher_name="Test Pitcher", pitcher_id=1, team="NYY", opponent="BOS",
        game_pk=1, k_pct_30d=0.30, k_pct_season=0.25, ip_last5_mean=6.0,
        starts=10, opp_k_pct_l15=0.228, ump_k_factor=1.0, park_k_factor=1.0)
    assert p.k_pct_blended == pytest.approx(0.285)
    assert p.bf_expected == pytest.approx(25.8)
    assert p.projected_ks == pytest.approx(7.35, abs=0.01)


def test_edge_and_side():
    from model import scoring
    p = scoring.project_strikeouts(
        pitcher_name="T", pitcher_id=1, team="A", opponent="B", game_pk=1,
        k_pct_30d=0.30, k_pct_season=0.25, ip_last5_mean=6.0, starts=10,
        opp_k_pct_l15=0.228, ump_k_factor=1.0, park_k_factor=1.0)
    scoring.apply_line(p, 6.5)
    assert p.side == "OVER" and p.edge == pytest.approx(0.85, abs=0.01)
    scoring.apply_line(p, 8.5)
    assert p.side == "UNDER" and p.edge < 0


def test_opp_factor_clamped():
    from model import scoring
    p = scoring.project_strikeouts(
        pitcher_name="T", pitcher_id=1, team="A", opponent="B", game_pk=1,
        k_pct_30d=0.25, k_pct_season=0.25, ip_last5_mean=6.0, starts=5,
        opp_k_pct_l15=0.50, ump_k_factor=1.0, park_k_factor=1.0)
    assert p.opp_factor == 1.30  # clamped upper bound


def test_hr_projection_probability():
    from model import scoring
    p = scoring.project_home_run(
        batter_name="Slugger", batter_id=2, team="NYY", opponent="BOS",
        game_pk=1, hr_pa_30d=0.06, hr_pa_season=0.05, park_hr_factor=1.0,
        pitcher_hr_per_bf=None, temp_f=None)
    # blend = .057; P = 1-(1-.057)^4.1 ≈ 0.214
    assert 0.18 < p.hr_prob < 0.25
    scoring.apply_hr_price(p, +350)
    assert p.implied_prob == pytest.approx(100 / 450, abs=0.001)
    assert p.edge == pytest.approx(p.hr_prob - p.implied_prob, abs=0.001)


def test_implied_prob():
    from fetchers.odds_api import implied_prob
    assert implied_prob(-110) == pytest.approx(110 / 210)
    assert implied_prob(+150) == pytest.approx(100 / 250)


def test_moneyline_win_prob_and_edge():
    from model import scoring
    # Even teams, no starter info: home team gets exactly the HFA bump.
    rec = {"w": 45, "l": 45, "rs": 400, "ra": 400}
    prob, breakdown = scoring.project_home_win_prob(
        home_rec=rec, away_rec=rec, home_kbb=None, away_kbb=None)
    assert prob == pytest.approx(0.535, abs=0.001)
    assert breakdown["log5"] == pytest.approx(0.5, abs=0.001)

    # Strong home team + better starter beats a weak road team convincingly.
    strong = {"w": 55, "l": 35, "rs": 480, "ra": 380}
    weak = {"w": 35, "l": 55, "rs": 370, "ra": 460}
    prob2, _ = scoring.project_home_win_prob(
        home_rec=strong, away_rec=weak, home_kbb=0.20, away_kbb=0.10)
    assert prob2 > 0.62

    p = scoring.MLProjection(team="TEX", team_id=140, opponent="LAA",
                             game_pk=1, win_prob=round(prob2, 4), is_home=True)
    scoring.apply_ml_price(p, +105)
    assert p.implied_prob == pytest.approx(100 / 205, abs=0.001)
    assert p.edge == pytest.approx(p.win_prob - p.implied_prob, abs=0.001)


def test_ml_confidence_and_grading():
    from model import confidence, scoring
    p = scoring.MLProjection(team="TEX", team_id=140, opponent="LAA",
                             game_pk=1, win_prob=0.58, is_home=True)
    scoring.apply_ml_price(p, +120)   # implied ~0.4545 -> edge ~0.125
    confidence.score_ml_pick(p, both_starters_named=True, domed_park=True,
                             sample_games=90)
    # base2 + edge_gte_0.09→4 + sample1 + starters1 + dome1 = 9
    assert p.confidence == 9

    # Grading: ML pick HIT when the stored team_id wins.
    import grader
    from unittest.mock import patch
    row = {"pick_type": "ML", "game_pk": 7, "pitcher_id": 140,
           "book_line": None, "pick_side": "ML"}
    with patch.object(grader.mlb, "game_final", return_value=True), \
         patch.object(grader.mlb, "game_winner", return_value=140):
        assert grader.grade_pick(row) == ("HIT", None)
    with patch.object(grader.mlb, "game_final", return_value=True), \
         patch.object(grader.mlb, "game_winner", return_value=108):
        assert grader.grade_pick(row) == ("MISS", None)


# --- confidence ------------------------------------------------------------------

def _proj(edge: float, starts: int = 10):
    from model import scoring
    p = scoring.project_strikeouts(
        pitcher_name="T", pitcher_id=1, team="A", opponent="B", game_pk=1,
        k_pct_30d=0.30, k_pct_season=0.25, ip_last5_mean=6.0, starts=starts,
        opp_k_pct_l15=0.228, ump_k_factor=1.03, park_k_factor=1.0)
    p.edge, p.side, p.book_line = edge, "OVER" if edge > 0 else "UNDER", 6.5
    return p


def test_confidence_tiers_and_cap():
    from model import confidence
    p = confidence.score_k_pick(
        _proj(1.6), domed_park=True, ump_confirmed=True, ump_high_k=True,
        injury_flags=False, csw_aligned=True, line_move=None)
    # base2 + edge4 + starts1 + dome1 + ump1 + umphighk1 + noinj1 + csw1 = 12 → cap 10
    assert p.confidence == 10
    assert p.conf_reasons["edge_gte_1_5"] == 4

    p2 = confidence.score_k_pick(
        _proj(0.8, starts=3), domed_park=False, ump_confirmed=False,
        ump_high_k=False, injury_flags=True, csw_aligned=None, line_move=None)
    # base2 + edge0.7tier 2 = 4
    assert p2.confidence == 4


def test_line_move_against_us_penalty():
    from model import confidence
    base = confidence.score_k_pick(
        _proj(1.0), domed_park=False, ump_confirmed=False, ump_high_k=False,
        injury_flags=False, csw_aligned=None, line_move=None).confidence
    faded = confidence.score_k_pick(
        _proj(1.0), domed_park=False, ump_confirmed=False, ump_high_k=False,
        injury_flags=False, csw_aligned=None, line_move=-0.5).confidence
    assert faded == base - 2


# --- grading ---------------------------------------------------------------------

def test_grade_k_outcomes():
    from grader import grade_k, units_for
    assert grade_k(9, 7.5, "OVER") == "HIT"
    assert grade_k(8, 6.5, "UNDER") == "MISS"
    assert grade_k(8, 8.0, "OVER") == "PUSH"
    assert grade_k(4, 5.5, "UNDER") == "HIT"
    assert grade_k(None, 5.5, "OVER") == "VOID"
    assert units_for("HIT", -110) == pytest.approx(0.909, abs=0.001)
    assert units_for("HIT", +120) == pytest.approx(1.2)
    assert units_for("MISS", -110) == -1.0
    assert units_for("PUSH", -110) == 0.0


# --- db round trips ---------------------------------------------------------------

def test_pick_roundtrip_and_summary():
    import db
    pid = db.insert_pick({
        "date": "2026-07-06", "pick_type": "K", "pitcher_name": "Paul Skenes",
        "pitcher_id": 694973, "team": "PIT", "opponent": "STL", "game_pk": 1,
        "first_pitch_utc": "2026-07-06T23:10:00Z", "book_line": 7.5,
        "best_book_name": "FanDuel", "best_book_price": -115,
        "my_projection": 8.6, "edge": 1.1, "confidence": 8,
        "pick_side": "OVER", "narrative": "x", "metrics_json": "{}",
    })
    db.set_result(pid, "HIT", 9, 0.87)
    db.upsert_daily_summary("2026-07-06", 1, 1, 0, 0, 0.87)
    row = db.picks_for_player("Skenes")[0]
    assert row["result"] == "HIT" and row["actual_ks"] == 9
    summ = db.summary_for("2026-07-06")
    assert summ["cumulative_units"] == pytest.approx(0.87)

    db.record_line(pid, 8.0, -120)
    assert db.latest_line(pid)["line"] == 8.0


def test_formula_change_and_skip_logs():
    import db
    db.log_formula_change("thresholds.min_edge", "0.7", "0.8", "test")
    db.add_skip("COL")
    assert "COL" in db.skips_for_today()
    db.add_force("Paul Skenes")
    assert "Paul Skenes" in db.force_adds_for_today()


def test_cost_tracking():
    import costs
    import db
    usd = costs.estimate_usd("claude-opus-4-7", 1000, 200)
    assert usd == pytest.approx((1000 * 5 + 200 * 25) / 1e6)
    db.record_cost("claude-opus-4-7", 1000, 200, usd)
    assert db.month_spend() == pytest.approx(usd)


# --- helpers -----------------------------------------------------------------------

def test_ip_notation():
    from fetchers.mlb_stats_api import _ip_to_float
    assert _ip_to_float("6.1") == pytest.approx(6 + 1 / 3)
    assert _ip_to_float("6.2") == pytest.approx(6 + 2 / 3)
    assert _ip_to_float(7) == 7.0


def test_name_normalization():
    from fetchers.odds_api import norm_name
    assert norm_name("José Berríos") == norm_name("Jose Berrios")
    assert norm_name("J.P. France") == norm_name("JP France")


def test_dispatch_schedule_matches_workflow():
    """Every cron in daily.yml must have a dispatch route, and vice versa."""
    import yaml
    sys.path.insert(0, str(ROOT / "scripts"))
    import dispatch
    with open(ROOT / ".github/workflows/daily.yml") as fh:
        wf = yaml.safe_load(fh)
    crons = {item["cron"] for item in wf[True]["schedule"]}  # 'on' parses as True
    assert crons == set(dispatch.SCHEDULE_MAP.keys())


def test_ml_board_renders():
    import cards
    from model import scoring
    projs = []
    for pk, team, opp, prob, price, home in (
        (1, "MIL", "STL", 0.65, -123, False),
        (2, "MIA", "SEA", 0.52, +118, True),
        (3, "NYY", "TB", 0.55, -160, True),   # negative edge, still listed
    ):
        p = scoring.MLProjection(team=team, team_id=pk, opponent=opp,
                                 game_pk=pk, win_prob=prob, is_home=home)
        scoring.apply_ml_price(p, price)
        projs.append(p)
    board = cards.ml_board(projs, picked_pks={1})
    assert board is not None
    lines = board.split("\n")
    assert lines[2].startswith("🎯 ")            # picked game first (best edge)
    assert "MIL" in lines[2] and "65%" in lines[2]
    assert sum("@" in ln for ln in lines) == 3   # one row per game
    assert cards.ml_board([], set()) is None


def test_cards_render():
    import cards
    from model import scoring
    p = scoring.project_strikeouts(
        pitcher_name="Paul Skenes", pitcher_id=1, team="PIT", opponent="STL",
        game_pk=1, k_pct_30d=0.35, k_pct_season=0.32, ip_last5_mean=6.3,
        starts=18, opp_k_pct_l15=0.24, ump_k_factor=1.02, park_k_factor=1.0)
    scoring.apply_line(p, 7.5)
    p.confidence = 8
    block = cards.k_pick_block(0, p, "Test narrative.")
    assert "Paul Skenes" in block and "7.5" in block
    card = cards.morning_card([block], [], ["test flag"])
    assert "MLB K PICKS" in card and "test flag" in card
