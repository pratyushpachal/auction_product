"""Database reads shared by API endpoints and the optimizer."""
import json
from sqlalchemy import text


def rows(conn, sql, params=None):
    return [dict(row) for row in conn.execute(text(sql), params or {}).mappings()]


def read_meta(conn, key, default=None):
    value = conn.execute(text("SELECT value FROM pipeline_meta WHERE key=:key"), {"key": key}).scalar()
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def player_data(conn):
    memberships = {}
    for r in rows(conn, "SELECT DISTINCT mp.player_id, m.competition, m.season FROM match_players mp JOIN matches m USING(match_id)"):
        bucket = memberships.setdefault(r["player_id"], {"competitions": set(), "seasons": set(), "pairs": set()})
        bucket["competitions"].add(r["competition"])
        bucket["seasons"].add(r["season"])
        bucket["pairs"].add((r["competition"], r["season"]))
    result = []
    for r in rows(conn, "SELECT p.*, s.primary_role, s.score, s.ci_low, s.ci_high, s.rank, s.sample_balls, s.sample_innings, s.batting_balls, s.bowling_balls, s.archetype, s.adjustment_label, s.summary_json FROM players p LEFT JOIN player_scores s USING(player_id)"):
        membership = memberships.get(r["player_id"], {"competitions": set(), "seasons": set(), "pairs": set()})
        result.append({"id": r["player_id"], "name": r["canonical_name"], "role": r["primary_role"],
            **{k:r[k] for k in ("score", "ci_low", "ci_high", "rank", "sample_balls", "sample_innings", "batting_balls", "bowling_balls", "archetype", "adjustment_label", "state", "batting_hand", "bowling_type", "dob", "nationality")},
            "competitions": sorted(membership["competitions"]), "seasons": sorted(membership["seasons"]),
            "_pairs": membership["pairs"], "_metrics": json.loads(r["summary_json"]) if r["summary_json"] else {}})
    return result


def public(player):
    return {k:v for k,v in player.items() if not k.startswith("_")}


def filter_players(items, q=None, competition=None, season=None, role=None, archetype=None, min_score=None):
    result = []
    for p in items:
        if q and q.casefold() not in p["name"].casefold():
            continue
        if competition and competition not in p["competitions"]:
            continue
        if season and season not in p["seasons"]:
            continue
        if competition and season and (competition, season) not in p["_pairs"]:
            continue
        if role and p["role"] != role:
            continue
        if archetype and p["archetype"] != archetype:
            continue
        if min_score is not None and (p["score"] is None or p["score"] < min_score):
            continue
        result.append(p)
    return result


def report(player, items):
    similar = [] if player["score"] is None else [p for p in items if p["id"] != player["id"] and p["role"] == player["role"] and p["score"] is not None]
    similar.sort(key=lambda p: (abs(p["score"] - player["score"]), p["id"]))
    return {"player": public(player), "metrics": player["_metrics"], "similar": [public(p) for p in similar[:3]]}
