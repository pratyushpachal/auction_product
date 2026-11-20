"""Data-quality assertions against the uploaded files, never generated match data."""
import csv
import json
from collections import Counter

import pytest
from sqlalchemy import select, text

from kkr_scout.common import PROJECT_ROOT
from kkr_scout.database import TABLES, get_engine
from kkr_scout.identity import FUZZY_THRESHOLD, PlayerResolver
from kkr_scout.ingest import ingest
from kkr_scout.inventory import raw_files


@pytest.fixture(scope="module")
def engine():
    engine = get_engine()
    # The CI command ingests first. This also supports pytest on a fresh checkout.
    try:
        with engine.connect() as conn:
            loaded = conn.execute(text("SELECT COUNT(*) FROM matches")).scalar_one()
    except Exception:
        loaded = 0
    if not loaded:
        ingest(engine=engine)
    return engine


def test_upload_and_database_competition_counts(engine):
    expected = Counter()
    for path in raw_files(PROJECT_ROOT):
        doc = json.loads(path.read_text(encoding="utf-8"))
        expected[doc["info"]["event"]["name"]] += 1
    with engine.connect() as conn:
        actual = dict(conn.execute(text("SELECT competition,COUNT(*) FROM matches GROUP BY competition")).all())
    assert actual == dict(expected)


def test_idempotent_loader_preserves_counts_and_raw_hashes(engine):
    import hashlib
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in raw_files(PROJECT_ROOT)}
    first = ingest(engine=engine)
    second = ingest(engine=engine)
    for name in ("matches", "innings", "deliveries", "players", "wickets"):
        assert first[name] == second[name]
    assert second["processed"] == 0
    assert second["skipped"] == len(before)
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before}


def test_no_duplicate_matches_or_deliveries(engine):
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*)-COUNT(DISTINCT source_match_id) FROM matches")).scalar_one() == 0
        assert conn.execute(text('SELECT COUNT(*) FROM (SELECT match_id,innings_no,"over",ball FROM deliveries GROUP BY match_id,innings_no,"over",ball HAVING COUNT(*)>1) q')).scalar_one() == 0


def test_ball_flags_reconcile_source_and_no_ball_traps(engine):
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM deliveries WHERE (wides>0 OR noballs>0) AND (batter_ball<>0 OR is_legal<>0)")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM deliveries WHERE wides=0 AND noballs=0 AND (batter_ball<>1 OR is_legal<>1)")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM deliveries WHERE wides>0")).scalar_one() > 0
        assert conn.execute(text("SELECT COUNT(*) FROM deliveries WHERE noballs>0")).scalar_one() > 0
        rows = {(r.match_id, r.innings_no): (r.events, r.legal) for r in conn.execute(text("SELECT match_id,innings_no,COUNT(*) events,SUM(is_legal) legal FROM deliveries GROUP BY match_id,innings_no"))}
    for path in raw_files(PROJECT_ROOT):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for no, inn in enumerate(doc["innings"], 1):
            events = [d for o in inn["overs"] for d in o["deliveries"]]
            legal = sum(not (d.get("extras", {}).get("wides", 0) or d.get("extras", {}).get("noballs", 0)) for d in events)
            assert rows[("cricsheet:" + path.stem, no)] == (len(events), legal)


def test_bowler_conceded_runs_exclude_byes_and_legbyes(engine):
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM deliveries WHERE bowler_runs<>total_runs-byes-legbyes-penalty")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM deliveries WHERE byes>0 OR legbyes>0")).scalar_one() > 0


def test_totals_reconcile_against_uploaded_delivery_source(engine):
    with engine.connect() as conn:
        rows = {(r.match_id, r.innings_no): r.total_runs for r in conn.execute(select(TABLES["innings"]))}
        assert conn.execute(text("SELECT COUNT(*) FROM batting_scorecards")).scalar_one() == 0
        unavailable = conn.execute(text("SELECT value FROM pipeline_meta WHERE key='data_quality'")).scalar_one()
    assert any(r["status"] == "unavailable" and "Independent" in r["check"] for r in json.loads(unavailable))
    for path in raw_files(PROJECT_ROOT):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for no, inn in enumerate(doc["innings"], 1):
            penalty = inn.get("penalty_runs", {})
            total = sum(d["runs"]["total"] for o in inn["overs"] for d in o["deliveries"]) + penalty.get("pre", 0) + penalty.get("post", 0)
            assert rows[("cricsheet:" + path.stem, no)] == total


def test_null_and_orphan_ids(engine):
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM deliveries WHERE batter_id IS NULL OR non_striker_id IS NULL OR bowler_id IS NULL")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM deliveries d LEFT JOIN players b ON b.player_id=d.batter_id LEFT JOIN players n ON n.player_id=d.non_striker_id LEFT JOIN players o ON o.player_id=d.bowler_id WHERE b.player_id IS NULL OR n.player_id IS NULL OR o.player_id IS NULL")).scalar_one() == 0


def test_date_range_matches_upload_manifest(engine):
    with (PROJECT_ROOT / "data/upload_manifest.csv").open(encoding="utf-8", newline="") as handle:
        dates = [r["date"] for r in csv.DictReader(handle)]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT MIN(date),MAX(date) FROM matches")).one() == (min(dates), max(dates))


def test_super_overs_and_interrupted_match_metadata(engine):
    with engine.connect() as conn:
        supers = conn.execute(text("SELECT COUNT(*) FROM innings WHERE is_super_over=1")).scalar_one()
        assert supers > 0
        assert conn.execute(text("SELECT COUNT(*) FROM regulation_deliveries d JOIN innings i USING (match_id,innings_no) WHERE i.is_super_over=1")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM matches WHERE outcome_method IS NOT NULL")).scalar_one() > 0
        assert conn.execute(text("SELECT COUNT(*) FROM matches WHERE result='no result'")).scalar_one() > 0


def test_zero_indexed_overs_and_source_miscounts(engine):
    source_miscounts = set()
    for path in raw_files(PROJECT_ROOT):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for no, inn in enumerate(doc["innings"], 1):
            if inn["overs"]:
                assert inn["overs"][0]["over"] == 0
            source_miscounts.update(("cricsheet:" + path.stem, no, int(over)) for over in inn.get("miscounted_overs", {}))
    with engine.connect() as conn:
        actual = set(conn.execute(text('SELECT d.match_id,d.innings_no,d."over" FROM deliveries d JOIN matches m ON m.match_id=d.match_id GROUP BY d.match_id,d.innings_no,d."over",m.balls_per_over HAVING SUM(d.is_legal)>m.balls_per_over')).all())
    assert actual <= source_miscounts


def test_manual_override_applied_before_registry_identity(tmp_path):
    doc = json.loads(raw_files(PROJECT_ROOT)[0].read_text(encoding="utf-8"))
    registry = doc["info"]["registry"]["people"]
    names = doc["info"]["players"][doc["info"]["teams"][0]]
    name, target = names[:2]
    path = tmp_path / "manual_overrides.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["source_player_id", "target_player_id", "canonical_name"])
        writer.writeheader()
        writer.writerow({"source_player_id": registry[name], "target_player_id": registry[target], "canonical_name": target})
    resolver = PlayerResolver(path)
    assert resolver.resolve(name, registry, doc["info"]["event"]["name"], doc["info"]["teams"][0])[0] == "cricsheet:" + registry[target]


def test_no_fuzzy_merge_without_context_and_below_threshold():
    doc = json.loads(raw_files(PROJECT_ROOT)[0].read_text(encoding="utf-8"))
    info = doc["info"]
    team, other_team = info["teams"]
    name = info["players"][team][0]
    pid = "cricsheet:" + info["registry"]["people"][name]
    known = [{"name": name, "player_id": pid, "team": team, "season": str(info["season"])}]
    resolver = PlayerResolver(known=known)
    result = resolver.resolve(name, {}, info["event"]["name"], other_team, str(info["season"]))
    assert result[0].startswith("provisional:")
    assert resolver.review[0]["similarity"] < FUZZY_THRESHOLD
    assert resolver.resolve(name, {}, info["event"]["name"], team, str(info["season"]))[0] == pid
