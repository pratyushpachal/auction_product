"""Metrics checks use uploaded database observations rather than invented matches."""
import json

import numpy as np
import pytest
from sqlalchemy import text

from kkr_scout.database import get_engine
from kkr_scout.ingest import ingest
from kkr_scout.metrics import Aggregate, BATTING_MIN, BOWLING_MIN, PHASES, empirical_prior, kmeans_numpy, posterior, score


@pytest.fixture(scope="module")
def scored():
    engine = get_engine()
    try:
        with engine.connect() as conn:
            ready = conn.execute(text("SELECT COUNT(*) FROM player_scores")).scalar_one()
    except Exception:
        ready = 0
    if not ready:
        ingest(engine=engine)
        score()
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(text("SELECT * FROM player_scores")).mappings()]
        summary = json.loads(conn.execute(text("SELECT value FROM pipeline_meta WHERE key='model_summary'")).scalar_one())
        methodology = json.loads(conn.execute(text("SELECT value FROM pipeline_meta WHERE key='methodology'")).scalar_one())
    return engine, rows, summary, methodology


def test_every_batting_and_bowling_count_matches_regulation_sql(scored):
    engine, rows, summary, _ = scored
    with engine.connect() as conn:
        bat = {r.player_id: (r.balls, r.runs) for r in conn.execute(text("SELECT batter_id player_id,SUM(batter_ball) balls,SUM(runs_bat) runs FROM regulation_deliveries GROUP BY batter_id"))}
        bowl = {r.player_id: (r.balls, r.runs) for r in conn.execute(text("SELECT bowler_id player_id,SUM(is_legal) balls,SUM(bowler_runs) runs FROM regulation_deliveries GROUP BY bowler_id"))}
        assert summary["regulation_deliveries"] == conn.execute(text("SELECT COUNT(*) FROM regulation_deliveries")).scalar_one()
    for row in rows:
        data = json.loads(row["summary_json"])
        assert (data["batting"]["balls"], data["batting"]["runs"]) == bat.get(row["player_id"], (0, 0))
        assert (data["bowling"]["balls"], data["bowling"]["runs"]) == bowl.get(row["player_id"], (0, 0))


def test_every_used_context_is_supported_by_uploaded_samples(scored):
    engine, _, _, _ = scored
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM context_par WHERE run_rate IS NULL OR run_rate<=0 OR sample_balls<120 OR sample_innings<5")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM context_par")).scalar_one() > 0


def test_sample_thresholds_suppress_ranks_and_rate_insights(scored):
    _, rows, _, _ = scored
    for row in rows:
        data = json.loads(row["summary_json"])
        if row["batting_balls"] < BATTING_MIN and row["bowling_balls"] < BOWLING_MIN:
            assert row["score"] is None and row["rank"] is None and row["archetype"] is None
        for d, threshold in (("batting", BATTING_MIN), ("bowling", BOWLING_MIN)):
            if data[d]["balls"] < threshold:
                assert data[d]["status"] == "Insufficient data"
                assert data[d]["adjusted_rating"] is None
            else:
                assert data[d]["ci_low"] <= data[d]["adjusted_rating"] <= data[d]["ci_high"]
                insight = data["insights"][0 if d == "batting" else 1]
                assert str(data[d]["balls"]) in insight
                assert "innings" in insight and "95% model interval" in insight


def test_phase_and_season_counts_partition_every_player(scored):
    _, rows, _, _ = scored
    for row in rows:
        data = json.loads(row["summary_json"])
        assert {r["phase_key"] for r in data["phases"]} == {p for p, _ in PHASES}
        for d in ("batting", "bowling"):
            for value in ("balls", "runs"):
                assert sum(p[d][value] for p in data["phases"]) == data[d][value]
                assert sum(s[d][value] for s in data["seasons"]) == data[d][value]


def test_dismissal_and_bowler_wicket_counts_match_sql(scored):
    engine, rows, _, _ = scored
    with engine.connect() as conn:
        dismissals = dict(conn.execute(text("SELECT w.dismissed_player_id,SUM(w.counts_as_dismissal) FROM wickets w JOIN innings i USING(match_id,innings_no) WHERE i.is_super_over=0 GROUP BY w.dismissed_player_id")).all())
        credited = dict(conn.execute(text('SELECT d.bowler_id,SUM(w.credited_to_bowler) FROM wickets w JOIN deliveries d ON d.match_id=w.match_id AND d.innings_no=w.innings_no AND d."over"=w."over" AND d.ball=w.ball JOIN innings i ON i.match_id=d.match_id AND i.innings_no=d.innings_no WHERE i.is_super_over=0 GROUP BY d.bowler_id')).all())
    for row in rows:
        data = json.loads(row["summary_json"])
        assert data["batting"]["dismissals"] == dismissals.get(row["player_id"], 0)
        assert data["bowling"]["wickets"] == credited.get(row["player_id"], 0)


def test_kmeans_is_deterministic_on_uploaded_phase_shares(scored):
    _, rows, _, _ = scored
    features = []
    for row in rows:
        if row["rank"] is not None:
            d = json.loads(row["summary_json"])
            features.append([p["batting"]["balls"] / max(d["batting"]["balls"], 1) for p in d["phases"]])
    first, centers = kmeans_numpy(features)
    second, other_centers = kmeans_numpy(features)
    assert np.array_equal(first, second)
    assert np.array_equal(centers, other_centers)
    assert len(np.unique(first)) == len(centers)
    assert np.isfinite(centers).all()


def test_empirical_bayes_pulls_uploaded_rates_toward_fitted_mean(scored):
    engine, _, _, _ = scored
    with engine.connect() as conn:
        groups = [Aggregate(balls=r.balls, runs=r.runs, adjusted_runs=r.runs) for r in conn.execute(text("SELECT SUM(batter_ball) balls,SUM(runs_bat) runs FROM regulation_deliveries GROUP BY batter_id HAVING SUM(batter_ball)>=30"))]
    prior = empirical_prior(groups)
    assert prior["players"] == len(groups)
    for g in groups:
        rating, low, high = posterior(g, prior, "batting")
        raw = 100 * g.runs / g.balls
        mean = 100 * prior["mean"]
        assert min(raw, mean) - 1e-6 <= rating <= max(raw, mean) + 1e-6
        assert low < rating < high


def test_backtest_scope_and_cutoff_are_from_uploaded_records(scored):
    engine, _, summary, methodology = scored
    test = summary["backtest"]
    with engine.connect() as conn:
        latest = conn.execute(text("SELECT MAX(date) FROM matches WHERE competition=:competition"), {"competition": test["competition"]}).scalar_one()
    assert test["holdout_year"] == latest[:4]
    assert test["train_before"] == latest[:4] + "-01-01"
    assert test["auction_test_status"] == "Impossible with uploaded data"
    assert "eligibility" in test["auction_test_reason"]
    assert methodology["backtest"] == test
    if test["status"] == "Measured descriptive season holdout":
        assert test["balls"] > 0 and test["innings"] > 0
        assert test["ci_low"] <= test["ci_high"]
        assert abs(test["difference"] - (test["top_group_sr"] - test["other_group_sr"])) < 0.001


def test_rank_order_and_all_rounder_intervals_are_consistent(scored):
    _, rows, summary, _ = scored
    ranked = sorted((r for r in rows if r["rank"] is not None), key=lambda r: r["rank"])
    assert [r["rank"] for r in ranked] == list(range(1, len(ranked) + 1))
    assert len(ranked) == summary["ranked_players"]
    assert all(a["score"] >= b["score"] for a, b in zip(ranked, ranked[1:]))
    for row in ranked:
        assert row["ci_low"] <= row["score"] <= row["ci_high"]
        if row["primary_role"] == "all_rounder":
            data = json.loads(row["summary_json"])
            assert abs(row["score"] - (data["batting"]["adjusted_rating"] + data["bowling"]["adjusted_rating"]) / 2) < 0.001
