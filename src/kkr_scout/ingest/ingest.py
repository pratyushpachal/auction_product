"""Load the inventoried Cricsheet uploads into one idempotent schema."""
import hashlib
import json
from collections import Counter
from pathlib import Path

from sqlalchemy import select, text

from ..common import PROJECT_ROOT, canonical_team, over_band, write_csv, write_json
from ..database import TABLES, create_schema, get_engine
from ..identity import PlayerResolver
from ..inventory import inventory, raw_files

NON_DISMISSALS = {"retired hurt"}
NON_BOWLER_WICKETS = {"run out", "retired hurt", "retired out", "obstructing the field"}


def j(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def set_meta(conn, key, value):
    table = TABLES["pipeline_meta"]
    conn.execute(table.delete().where(table.c.key == key))
    conn.execute(table.insert(), {"key": key, "value": j(value)})


def ingest(root: Path = PROJECT_ROOT, engine=None) -> dict:
    root = Path(root)
    source_inventory = inventory(root)
    engine = create_schema(engine or get_engine(root))
    overrides_path = root / "data/manual_overrides.csv"
    if not overrides_path.exists():
        write_csv(overrides_path, [], ["source_player_id", "target_player_id", "canonical_name", "source_name", "competition", "team", "season", "state"])
    known, docs = [], []
    for path in raw_files(root):
        raw = path.read_bytes()
        doc = json.loads(raw)
        docs.append((path, hashlib.sha256(raw).hexdigest(), doc))
        info = doc["info"]
        for team, names in info["players"].items():
            for name in names:
                pid = info.get("registry", {}).get("people", {}).get(name)
                if pid:
                    known.append({"name": name, "player_id": "cricsheet:" + pid, "team": team, "season": str(info["season"]), "competition": info["event"]["name"]})
    resolver = PlayerResolver(overrides_path, known)
    review_path = root / "data/review_bucket.csv"
    if review_path.exists():
        import csv
        with review_path.open(encoding="utf-8", newline="") as handle:
            resolver.review = list(csv.DictReader(handle))
    override_digest = hashlib.sha256(overrides_path.read_bytes()).hexdigest()
    processed, skipped = 0, 0
    with engine.begin() as conn:
        existing = {r.match_id: r.source_sha256 for r in conn.execute(select(TABLES["matches"].c.match_id, TABLES["matches"].c.source_sha256))}
        players = {r.player_id: dict(r._mapping) for r in conn.execute(select(TABLES["players"]))}
        aliases = {(r.source_player_id, r.alias_name): dict(r._mapping) for r in conn.execute(select(TABLES["player_aliases"]))}
        old_override = conn.execute(select(TABLES["pipeline_meta"].c.value).where(TABLES["pipeline_meta"].c.key == "ingest_override_sha256")).scalar_one_or_none()
        override_changed = old_override is not None and json.loads(old_override) != override_digest
        seen_hashes = set()
        for path, digest, doc in docs:
            if digest in seen_hashes:
                skipped += 1
                continue
            seen_hashes.add(digest)
            match_id = "cricsheet:" + path.stem
            if existing.get(match_id) == digest and not override_changed:
                skipped += 1
                continue
            if match_id in existing:
                for child in ("wickets", "deliveries", "batting_scorecards", "bowling_scorecards", "innings", "match_players"):
                    conn.execute(TABLES[child].delete().where(TABLES[child].c.match_id == match_id))
                conn.execute(TABLES["matches"].delete().where(TABLES["matches"].c.match_id == match_id))
            info = doc["info"]
            competition, season, match_date = info["event"]["name"], str(info["season"]), str(info["dates"][0])
            teams = info["teams"]
            registry = info.get("registry", {}).get("people", {})
            outcome, toss = info.get("outcome", {}), info.get("toss", {})
            conn.execute(TABLES["matches"].insert(), {
                "match_id": match_id, "source": "cricsheet", "source_match_id": path.stem, "source_file": path.relative_to(root).as_posix(), "source_sha256": digest, "data_version": doc["meta"]["data_version"],
                "competition": competition, "season": season, "date": match_date, "dates_json": j(info["dates"]), "gender": info["gender"], "match_type": info["match_type"],
                "venue": info.get("venue"), "venue_id": info.get("registry", {}).get("venues", {}).get(info.get("venue")), "city": info.get("city"), "team1": teams[0], "team2": teams[1],
                "team1_canonical": canonical_team(teams[0]), "team2_canonical": canonical_team(teams[1]), "balls_per_over": info["balls_per_over"], "scheduled_overs": info.get("overs"),
                "toss_winner": toss.get("winner"), "toss_decision": toss.get("decision"), "winner": outcome.get("winner"), "result": outcome.get("result"), "outcome_method": outcome.get("method"), "outcome_json": j(outcome), "missing_json": j(info.get("missing", [])), "info_json": j(info),
            })
            roster_rows = {}
            cache = {}
            def resolve(name, team):
                key = (name, team)
                if key in cache:
                    return cache[key]
                pid, source_id, canonical = resolver.resolve(name, registry, competition, team, season)
                cache[key] = pid
                player = players.get(pid)
                if player is None:
                    player = {"player_id": pid, "canonical_name": canonical or name, "name_as_of": match_date, "identity_status": "provisional" if pid.startswith("provisional:") else "registered", "state": None, "batting_hand": None, "bowling_type": None, "dob": None, "nationality": None}
                    conn.execute(TABLES["players"].insert(), player)
                    players[pid] = player
                elif match_date >= player["name_as_of"]:
                    player.update(canonical_name=canonical or name, name_as_of=match_date)
                    conn.execute(TABLES["players"].update().where(TABLES["players"].c.player_id == pid).values(canonical_name=player["canonical_name"], name_as_of=match_date))
                alias_key = (source_id or pid, name)
                alias = aliases.get(alias_key)
                if alias is None:
                    alias = {"source_player_id": source_id or pid, "alias_name": name, "player_id": pid, "first_seen": match_date, "last_seen": match_date}
                    conn.execute(TABLES["player_aliases"].insert(), alias)
                    aliases[alias_key] = alias
                else:
                    alias.update(first_seen=min(alias["first_seen"], match_date), last_seen=max(alias["last_seen"], match_date))
                    conn.execute(TABLES["player_aliases"].update().where((TABLES["player_aliases"].c.source_player_id == alias_key[0]) & (TABLES["player_aliases"].c.alias_name == name)).values(first_seen=alias["first_seen"], last_seen=alias["last_seen"], player_id=pid))
                roster_rows[(pid, team)] = {"match_id": match_id, "player_id": pid, "team": team, "team_canonical": canonical_team(team), "source_name": name}
                return pid
            for team, names in info["players"].items():
                for name in names:
                    resolve(name, team)
            for innings_no, inn in enumerate(doc["innings"], 1):
                batting_team = inn["team"]
                bowling_team = next(t for t in teams if t != batting_team)
                deliveries, wickets = [], []
                for over in inn["overs"]:
                    source_over = over["over"]
                    for ball_no, ball in enumerate(over["deliveries"], 1):
                        runs, extras = ball["runs"], ball.get("extras", {})
                        illegal = bool(extras.get("wides", 0) or extras.get("noballs", 0))
                        row = {
                            "match_id": match_id, "innings_no": innings_no, "over": source_over, "ball": ball_no, "delivery_no": len(deliveries) + 1, "actual_delivery": str(ball["actual_delivery"]) if "actual_delivery" in ball else None,
                            "batter_id": resolve(ball["batter"], batting_team), "non_striker_id": resolve(ball["non_striker"], batting_team), "bowler_id": resolve(ball["bowler"], bowling_team),
                            "runs_bat": runs["batter"], "extras": runs["extras"], "total_runs": runs["total"], "wides": extras.get("wides", 0), "noballs": extras.get("noballs", 0), "byes": extras.get("byes", 0), "legbyes": extras.get("legbyes", 0), "penalty": extras.get("penalty", 0),
                            "is_legal": int(not illegal), "batter_ball": int(not illegal), "bowler_runs": runs["total"] - extras.get("byes", 0) - extras.get("legbyes", 0) - extras.get("penalty", 0),
                            "is_boundary": int(runs["batter"] in (4, 6) and not runs.get("non_boundary", False)), "over_band": over_band(source_over), "extras_json": j(extras),
                        }
                        if row["runs_bat"] + row["extras"] != row["total_runs"] or sum(extras.values()) != row["extras"]:
                            raise ValueError(f"Delivery runs do not reconcile in {path.name}, innings {innings_no}, over {source_over}, event {ball_no}")
                        deliveries.append(row)
                        for wicket_no, wicket in enumerate(ball.get("wickets", []), 1):
                            for fielder in wicket.get("fielders", []):
                                resolve(fielder["name"], bowling_team)
                            wickets.append({"match_id": match_id, "innings_no": innings_no, "over": source_over, "ball": ball_no, "wicket_no": wicket_no, "dismissed_player_id": resolve(wicket["player_out"], batting_team), "wicket_type": wicket["kind"], "counts_as_dismissal": int(wicket["kind"] not in NON_DISMISSALS), "credited_to_bowler": int(wicket["kind"] not in NON_BOWLER_WICKETS), "fielders_json": j(wicket.get("fielders", []))})
                legal = sum(d["is_legal"] for d in deliveries)
                penalty = inn.get("penalty_runs", {})
                conn.execute(TABLES["innings"].insert(), {"match_id": match_id, "innings_no": innings_no, "batting_team": batting_team, "batting_team_canonical": canonical_team(batting_team), "bowling_team": bowling_team, "is_super_over": int(bool(inn.get("super_over", False))), "total_runs": sum(d["total_runs"] for d in deliveries) + penalty.get("pre", 0) + penalty.get("post", 0), "legal_balls": legal, "delivery_count": len(deliveries), "wickets": sum(w["counts_as_dismissal"] for w in wickets), "overs_text": f"{legal // info['balls_per_over']}.{legal % info['balls_per_over']}", "penalty_pre": penalty.get("pre", 0), "penalty_post": penalty.get("post", 0), "target_runs": inn.get("target", {}).get("runs"), "target_overs": inn.get("target", {}).get("overs"), "metadata_json": j({k: v for k, v in inn.items() if k != "overs"})})
                if deliveries:
                    conn.execute(TABLES["deliveries"].insert(), deliveries)
                if wickets:
                    conn.execute(TABLES["wickets"].insert(), wickets)
            if roster_rows:
                conn.execute(TABLES["match_players"].insert(), list(roster_rows.values()))
            processed += 1
        set_meta(conn, "inventory", source_inventory)
        set_meta(conn, "ingest_override_sha256", override_digest)
        set_meta(conn, "source_policy", {"raw_files": "Untouched uploaded JSON", "tier_b": "No Tier B uploads", "rosters": "Historical match participants; not the current squad", "over_index": "Source over 0 is first over", "batter_balls": "Wides and no-balls excluded as requested", "super_overs": "Stored separately and excluded from regulation metrics", "interrupted_matches": "Stored with source outcome.method and result; par adjustment excludes interrupted matches"})
    with engine.connect() as conn:
        provisional_ids = set(conn.execute(text("SELECT DISTINCT p.player_id FROM players p JOIN match_players mp ON p.player_id=mp.player_id WHERE p.identity_status='provisional'")).scalars())
    resolver.review = [row for row in resolver.review if row["provisional_id"] in provisional_ids]
    resolver.write_review(review_path)
    quality = quality_report(engine)
    with engine.begin() as conn:
        set_meta(conn, "data_quality", quality)
    write_json(root / "data/data_quality.json", quality)
    with engine.connect() as conn:
        counts = {name: conn.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar_one() for name in ("matches", "innings", "deliveries", "players", "wickets", "batting_scorecards", "bowling_scorecards")}
    return {**counts, "processed": processed, "skipped": skipped, "quality": quality}


def quality_report(engine):
    checks = [
        ("Duplicate source match IDs", "SELECT COUNT(*) FROM (SELECT source,source_match_id FROM matches GROUP BY source,source_match_id HAVING COUNT(*)>1) q"),
        ("Duplicate delivery keys", 'SELECT COUNT(*) FROM (SELECT match_id,innings_no,"over",ball FROM deliveries GROUP BY match_id,innings_no,"over",ball HAVING COUNT(*)>1) q'),
        ("Invalid run reconciliation", "SELECT COUNT(*) FROM deliveries WHERE runs_bat+extras<>total_runs OR wides+noballs+byes+legbyes+penalty<>extras"),
        ("Invalid legal ball flags", "SELECT COUNT(*) FROM deliveries WHERE is_legal<>CASE WHEN wides>0 OR noballs>0 THEN 0 ELSE 1 END OR batter_ball<>is_legal"),
        ("Invalid bowler conceded runs", "SELECT COUNT(*) FROM deliveries WHERE bowler_runs<>total_runs-byes-legbyes-penalty"),
        ("Innings total or ball count mismatch", "SELECT COUNT(*) FROM innings i LEFT JOIN (SELECT match_id,innings_no,SUM(total_runs) runs,SUM(is_legal) balls,COUNT(*) events FROM deliveries GROUP BY match_id,innings_no) d ON i.match_id=d.match_id AND i.innings_no=d.innings_no WHERE i.total_runs<>COALESCE(d.runs,0)+i.penalty_pre+i.penalty_post OR i.legal_balls<>COALESCE(d.balls,0) OR i.delivery_count<>COALESCE(d.events,0)"),
        ("Null player references", "SELECT COUNT(*) FROM deliveries WHERE batter_id IS NULL OR non_striker_id IS NULL OR bowler_id IS NULL"),
        ("Orphan player references", "SELECT COUNT(*) FROM deliveries d LEFT JOIN players a ON d.batter_id=a.player_id LEFT JOIN players b ON d.non_striker_id=b.player_id LEFT JOIN players c ON d.bowler_id=c.player_id WHERE a.player_id IS NULL OR b.player_id IS NULL OR c.player_id IS NULL"),
        ("Invalid dates", "SELECT COUNT(*) FROM matches WHERE date<'1900-01-01' OR date>'2026-11-20'"),
    ]
    with engine.connect() as conn:
        results = [{"check": label, "failures": conn.execute(text(sql)).scalar_one(), "status": "pass"} for label, sql in checks]
        for row in results:
            if row["failures"]:
                row["status"] = "fail"
        overcounts = conn.execute(text('SELECT COUNT(*) FROM (SELECT d.match_id,d.innings_no,d."over",SUM(d.is_legal) n,m.balls_per_over FROM deliveries d JOIN matches m ON m.match_id=d.match_id GROUP BY d.match_id,d.innings_no,d."over",m.balls_per_over HAVING SUM(d.is_legal)>m.balls_per_over) q')).scalar_one()
    results.append({"check": "Overs exceeding scheduled legal-ball count", "failures": overcounts, "status": "source exception", "detail": "Inspect innings metadata.miscounted_overs; preserve source umpire miscounts."})
    results.append({"check": "Independent scorecard reconciliation", "failures": None, "status": "unavailable", "detail": "No second-source scorecard was uploaded. Source delivery sums are checked; they are not independent verification."})
    return results
