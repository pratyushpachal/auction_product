"""Batch-only descriptive metrics and empirical-Bayes ratings from uploaded deliveries."""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.stats import gamma, t
from sqlalchemy import Column, Float, ForeignKey, Integer, String, Table, Text, delete, select, text

from ..common import PROJECT_ROOT
from ..database import TABLES, get_engine, metadata

BATTING_MIN = 150
BOWLING_MIN = 120
PHASE_MIN = 30
PAR_MIN_INNINGS = 5
PAR_MIN_BALLS = 120
BRIDGE_MIN = 10
HOLDOUT_MIN_PLAYERS = 10
PHASES = (
    ("overs_1_6", "Overs 1 to 6"),
    ("overs_7_15", "Overs 7 to 15"),
    ("overs_16_plus", "Overs 16 onward"),
)
BAT_ROLES = {"overs_1_6": "opener", "overs_7_15": "middle_order", "overs_16_plus": "finisher"}
BOWL_ROLES = {"overs_1_6": "powerplay_bowler", "overs_7_15": "middle_overs_bowler", "overs_16_plus": "death_bowler"}

player_scores = Table(
    "player_scores", metadata,
    Column("player_id", String(255), ForeignKey("players.player_id"), primary_key=True),
    Column("primary_role", String(255), nullable=False),
    Column("score", Float), Column("ci_low", Float), Column("ci_high", Float), Column("rank", Integer),
    Column("sample_balls", Integer, nullable=False), Column("sample_innings", Integer, nullable=False),
    Column("batting_balls", Integer, nullable=False), Column("bowling_balls", Integer, nullable=False),
    Column("archetype", String(255)), Column("adjustment_label", Text, nullable=False),
    Column("summary_json", Text, nullable=False),
)
player_metrics = Table(
    "player_metrics", metadata,
    Column("player_id", String(255), ForeignKey("players.player_id"), primary_key=True),
    Column("discipline", String(255), primary_key=True), Column("competition", String(255), primary_key=True),
    Column("season", String(255), primary_key=True), Column("phase", String(255), primary_key=True),
    Column("metric_json", Text, nullable=False),
)
context_par = Table(
    "context_par", metadata,
    Column("competition", String(255), primary_key=True), Column("season", String(255), primary_key=True),
    Column("venue", Text, primary_key=True), Column("phase", String(255), primary_key=True),
    Column("discipline", String(255), primary_key=True), Column("sample_balls", Integer, nullable=False),
    Column("sample_innings", Integer, nullable=False), Column("run_rate", Float),
    Column("ci_low", Float), Column("ci_high", Float), Column("fallback", Text, nullable=False),
)
TABLES.update(player_scores=player_scores, player_metrics=player_metrics, context_par=context_par)


def _number(value, precision=4):
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), precision)


def _dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


@dataclass
class Aggregate:
    balls: int = 0
    runs: int = 0
    adjusted_runs: float = 0.0
    boundaries: int = 0
    dots: int = 0
    wickets: int = 0
    innings: set = field(default_factory=set)

    def add(self, row, discipline, factor=1.0, wickets=0):
        legal = row["batter_ball"] if discipline == "batting" else row["is_legal"]
        runs = row["runs_bat"] if discipline == "batting" else row["bowler_runs"]
        self.balls += legal
        self.runs += runs
        self.adjusted_runs += runs * factor
        self.boundaries += int(bool(legal and row["is_boundary"]))
        self.dots += int(bool(legal and runs == 0))
        self.wickets += wickets
        self.innings.add((row["match_id"], row["innings_no"]))

    def merge(self, other):
        self.balls += other.balls
        self.runs += other.runs
        self.adjusted_runs += other.adjusted_runs
        self.boundaries += other.boundaries
        self.dots += other.dots
        self.wickets += other.wickets
        self.innings.update(other.innings)
        return self


def kmeans_numpy(values, k=4, max_iter=100):
    """Deterministic Lloyd k-means, with farthest-observation initialization."""
    values = np.asarray(values, dtype=float)
    if len(values) == 0:
        return np.empty(0, dtype=int), np.empty((0, 0))
    if values.ndim != 2 or not np.isfinite(values).all():
        raise ValueError("Archetype inputs must be a finite matrix")
    sd = values.std(axis=0)
    sd[sd == 0] = 1.0
    standardized = (values - values.mean(axis=0)) / sd
    k = min(int(k), len(np.unique(standardized, axis=0)))
    centers = [standardized[np.argmin(np.sum(standardized * standardized, axis=1))]]
    while len(centers) < k:
        distance = np.min(np.sum((standardized[:, None] - np.array(centers)[None]) ** 2, axis=2), axis=1)
        centers.append(standardized[np.argmax(distance)])
    centers = np.asarray(centers)
    labels = np.full(len(values), -1, dtype=int)
    for _ in range(max_iter):
        next_labels = np.argmin(np.sum((standardized[:, None] - centers[None]) ** 2, axis=2), axis=1)
        if np.array_equal(labels, next_labels):
            break
        labels = next_labels
        for cluster in range(k):
            members = standardized[labels == cluster]
            if len(members):
                centers[cluster] = members.mean(axis=0)
    return labels, centers


def empirical_prior(groups):
    """Estimate Gamma prior moments after subtracting Poisson sampling variance."""
    qualified = [g for g in groups if g.balls >= PHASE_MIN]
    if not qualified:
        return {"mean": None, "exposure": None, "players": 0, "status": "Insufficient data"}
    total_balls = sum(g.balls for g in qualified)
    mean = sum(g.adjusted_runs for g in qualified) / total_balls
    rates = np.array([g.adjusted_runs / g.balls for g in qualified])
    noise = np.array([max(g.adjusted_runs / g.balls, 0.0) / g.balls for g in qualified])
    between = max(float(np.var(rates, ddof=1)) - float(noise.mean()), 0.0) if len(rates) > 1 else 0.0
    # Cap exposure to the published batting minimum when estimated heterogeneity vanishes.
    exposure = min(mean / between, BATTING_MIN) if between > 0 else BATTING_MIN
    return {"mean": _number(mean, 8), "exposure": _number(exposure, 8), "players": len(qualified), "status": "Estimated from uploaded observations"}


def posterior(g, prior, discipline):
    if not g.balls or prior["mean"] is None or prior["mean"] <= 0:
        return None, None, None
    shape = g.adjusted_runs + prior["mean"] * prior["exposure"]
    exposure = g.balls + prior["exposure"]
    if shape <= 0:
        return None, None, None
    rate = shape / exposure
    low, high = gamma.ppf([0.025, 0.975], shape, scale=1 / exposure)
    if discipline == "bowling":
        return 100 / rate, 100 / high, 100 / low
    return 100 * rate, 100 * low, 100 * high


def metric(g, discipline, minimum, prior):
    enough = g.balls >= minimum
    result = {
        "balls": g.balls, "runs": g.runs, "innings": len(g.innings),
        "minimum_balls": minimum, "status": "Available" if enough else "Insufficient data",
        "dot_pct": _number(100 * g.dots / g.balls) if enough else None,
        "adjusted_rating": None, "ci_low": None, "ci_high": None,
    }
    if discipline == "batting":
        result.update(
            dismissals=g.wickets,
            sr=_number(100 * g.runs / g.balls) if enough else None,
            average=_number(g.runs / g.wickets) if enough and g.wickets else None,
            balls_per_dismissal=_number(g.balls / g.wickets) if enough and g.wickets else None,
            boundary_pct=_number(100 * g.boundaries / g.balls) if enough else None,
        )
    else:
        result.update(
            wickets=g.wickets,
            economy=_number(6 * g.runs / g.balls) if enough else None,
            strike_rate=_number(g.balls / g.wickets) if enough and g.wickets else None,
            wickets_per_24=_number(24 * g.wickets / g.balls) if enough else None,
        )
    if enough:
        rating, low, high = posterior(g, prior, discipline)
        result.update(adjusted_rating=_number(rating), ci_low=_number(low), ci_high=_number(high))
    return result


def _read(engine):
    with engine.connect() as conn:
        players = list(conn.execute(select(TABLES["players"])).mappings())
        matches = {r["match_id"]: dict(r) for r in conn.execute(select(TABLES["matches"])).mappings()}
        innings = {(r["match_id"], r["innings_no"]): dict(r) for r in conn.execute(select(TABLES["innings"])).mappings()}
        rows = [dict(r) for r in conn.execute(text("""SELECT d.*, m.competition, m.season, m.date, m.venue
            FROM deliveries d JOIN matches m ON m.match_id=d.match_id
            JOIN innings i ON i.match_id=d.match_id AND i.innings_no=d.innings_no
            WHERE i.is_super_over=0 AND m.match_type='T20'
            ORDER BY m.date,d.match_id,d.innings_no,d.delivery_no""")).mappings()]
        wickets = [dict(r) for r in conn.execute(select(TABLES["wickets"])).mappings()]
    return players, matches, innings, rows, wickets


def _contexts(rows, matches, innings):
    groups = defaultdict(Aggregate)
    for row in rows:
        match = matches[row["match_id"]]
        inn = innings[(row["match_id"], row["innings_no"])]
        clean = (not match["outcome_method"] and match["result"] != "no result"
                 and (match["scheduled_overs"] or 0) >= 20 and (inn["target_overs"] or 20) >= 20)
        if not clean:
            continue
        comp, season, venue, phase = row["competition"], row["season"], row["venue"] or "Not in uploaded data", row["over_band"]
        for discipline in ("batting", "bowling"):
            for key in ((comp, season, venue, phase, discipline), (comp, season, "*", phase, discipline),
                        (comp, "*", "*", phase, discipline), (comp, "*", "*", "*", discipline)):
                groups[key].add(row, discipline)
    used = {}
    for row in rows:
        comp, season, venue, phase = row["competition"], row["season"], row["venue"] or "Not in uploaded data", row["over_band"]
        for discipline in ("batting", "bowling"):
            key = (comp, season, venue, phase, discipline)
            if key in used:
                continue
            options = [key, (comp, season, "*", phase, discipline), (comp, "*", "*", phase, discipline), (comp, "*", "*", "*", discipline)]
            chosen = next((k for k in options if groups[k].balls >= PAR_MIN_BALLS and len(groups[k].innings) >= PAR_MIN_INNINGS and groups[k].runs), None)
            if chosen is None:
                used[key] = {"rate": None, "aggregate": Aggregate(), "fallback": "Insufficient data"}
            else:
                g = groups[chosen]
                used[key] = {"rate": g.runs / g.balls, "aggregate": g,
                             "fallback": ["Venue and season", "Competition and season", "Competition and phase", "Competition overall"][options.index(chosen)]}
    return used


def _wicket_counts(wickets, innings, rows):
    by_ball = {(r["match_id"], r["innings_no"], r["over"], r["ball"]): r for r in rows}
    dismissals, credited = defaultdict(int), defaultdict(int)
    for w in wickets:
        key = (w["match_id"], w["innings_no"], w["over"], w["ball"])
        row = by_ball.get(key)
        if row is None:
            continue
        if w["counts_as_dismissal"]:
            # Keep non-striker run-outs assigned to the dismissed batter, never the striker.
            dismissals[(key, w["dismissed_player_id"])] += 1
        if w["credited_to_bowler"]:
            credited[key] += 1
    return dismissals, credited


def _aggregate(rows, contexts, dismissals, credited, factors=None):
    total, phases, season_groups, league_year = (defaultdict(Aggregate) for _ in range(4))
    for row in rows:
        ballkey = (row["match_id"], row["innings_no"], row["over"], row["ball"])
        comp, season, venue, phase = row["competition"], row["season"], row["venue"] or "Not in uploaded data", row["over_band"]
        for discipline, player in (("batting", row["batter_id"]), ("bowling", row["bowler_id"])):
            rate = contexts[(comp, season, venue, phase, discipline)]["rate"]
            if rate is None or rate <= 0:
                raise ValueError("Insufficient data for the uploaded venue, season and phase context")
            factor = 1 / rate
            if factors is not None:
                factor *= factors[(comp, discipline)]
            wicket_count = dismissals[(ballkey, player)] if discipline == "batting" else credited[ballkey]
            total[(player, discipline)].add(row, discipline, factor, wicket_count)
            phases[(player, discipline, phase)].add(row, discipline, factor, wicket_count)
            season_groups[(player, discipline, comp, season)].add(row, discipline, factor, wicket_count)
            league_year[(player, discipline, comp, row["date"][:4])].add(row, discipline, factor, wicket_count)
        non_striker = row["non_striker_id"]
        if non_striker != row["batter_id"] and dismissals[(ballkey, non_striker)]:
            for g in (total[(non_striker, "batting")], phases[(non_striker, "batting", phase)],
                      season_groups[(non_striker, "batting", comp, season)], league_year[(non_striker, "batting", comp, row["date"][:4])]):
                g.wickets += dismissals[(ballkey, non_striker)]
                g.innings.add((row["match_id"], row["innings_no"]))
    return total, phases, season_groups, league_year


def _league_adjustment(league_year, competitions, reference):
    results = []
    factors = defaultdict(lambda: 1.0)
    for competition in competitions:
        if competition == reference:
            continue
        for discipline, minimum in (("batting", BATTING_MIN), ("bowling", BOWLING_MIN)):
            per_player = defaultdict(list)
            matched_keys = []
            for (player, kind, comp, year), target in league_year.items():
                if kind != discipline or comp != competition or target.balls < minimum:
                    continue
                ref = league_year.get((player, discipline, reference, year))
                if ref and ref.balls >= minimum and min(target.adjusted_runs, ref.adjusted_runs) > 0:
                    per_player[player].append(math.log((ref.adjusted_runs / ref.balls) / (target.adjusted_runs / target.balls)))
                    matched_keys.extend(((player, discipline, competition, year), (player, discipline, reference, year)))
            diffs = np.array([np.mean(v) for v in per_player.values()])
            item = {"competition": competition, "reference_competition": reference, "discipline": discipline,
                    "bridge_players": len(diffs), "minimum_bridge_players": BRIDGE_MIN,
                    "matched_calendar_years": sum(len(v) for v in per_player.values()),
                    "sample_balls": sum(league_year[key].balls for key in set(matched_keys)),
                    "factor": None, "ci_low": None, "ci_high": None}
            if len(diffs) >= BRIDGE_MIN:
                center = float(diffs.mean())
                se = float(diffs.std(ddof=1) / math.sqrt(len(diffs)))
                spread = float(t.ppf(0.975, len(diffs) - 1)) * se
                factors[(competition, discipline)] = math.exp(center)
                item.update(status="Bridge-adjusted descriptive metrics", factor=_number(math.exp(center)),
                            ci_low=_number(math.exp(center - spread)), ci_high=_number(math.exp(center + spread)))
            else:
                item.update(status="Insufficient bridge players; par-adjusted metrics")
            results.append(item)
    return factors, results


def _venue_par(matches, innings):
    groups = defaultdict(list)
    for key, inn in innings.items():
        m = matches[key[0]]
        if inn["is_super_over"] or inn["innings_no"] != 1 or m["outcome_method"] or m["result"] == "no result":
            continue
        if (m["scheduled_overs"] or 0) < 20 or not (inn["legal_balls"] >= 120 or inn["wickets"] >= 10):
            continue
        groups[(m["competition"], m["season"], m["venue"] or "Not in uploaded data")].append(inn["total_runs"])
    result = []
    for (comp, season, venue), runs in sorted(groups.items()):
        enough = len(runs) >= PAR_MIN_INNINGS
        par = np.mean(runs)
        spread = float(t.ppf(0.975, len(runs) - 1) * np.std(runs, ddof=1) / math.sqrt(len(runs))) if enough else None
        result.append({"competition": comp, "season": season, "venue": venue, "innings": len(runs),
                       "par_score": _number(par) if enough else None,
                       "ci_low": _number(par - spread) if enough else None, "ci_high": _number(par + spread) if enough else None,
                       "status": "Available" if enough else "Insufficient data"})
    return result


def _holdout(rows, matches, reference):
    reference_rows = [r for r in rows if r["competition"] == reference]
    latest_year = max((r["date"][:4] for r in reference_rows), default=None)
    result = {"auction_test_status": "Impossible with uploaded data",
              "auction_test_reason": "Auction dates, uncapped eligibility and sale records are absent. Match rosters do not establish auction eligibility.",
              "status": "Insufficient data", "competition": reference, "holdout_year": latest_year,
              "train_before": f"{latest_year}-01-01" if latest_year else None,
              "minimum_training_balls": BATTING_MIN, "minimum_holdout_balls": 60,
              "minimum_players": HOLDOUT_MIN_PLAYERS,
              "players": 0, "balls": 0, "innings": 0,
              "top_group_players": None, "top_group_sr": None, "other_group_sr": None,
              "difference": None, "ci_low": None, "ci_high": None, "correlation": None}
    train, held = defaultdict(Aggregate), defaultdict(Aggregate)
    for r in reference_rows:
        (held if r["date"][:4] == latest_year else train)[r["batter_id"]].add(r, "batting")
    ids = sorted(p for p in held if p in train and train[p].balls >= BATTING_MIN and held[p].balls >= 60)
    if len(ids) < HOLDOUT_MIN_PLAYERS:
        result.update(players=len(ids), balls=sum(held[p].balls for p in ids), innings=sum(len(held[p].innings) for p in ids),
                      reason=f"Insufficient data: {len(ids)} qualifying players; the descriptive holdout requires {HOLDOUT_MIN_PLAYERS}.")
        return result
    # Training league context and prior are derived before the holdout boundary.
    baseline = sum(g.runs for g in train.values()) / sum(g.balls for g in train.values())
    if baseline <= 0:
        result.update(status="Insufficient data", reason="Earlier uploaded innings have no positive scoring-rate baseline")
        return result
    for g in train.values():
        g.adjusted_runs = g.runs / baseline
    prior = empirical_prior(train.values())
    training_ratings = {p: posterior(train[p], prior, "batting")[0] for p in ids}
    ranked = sorted(ids, key=lambda p: (-training_ratings[p], p))
    top_n = math.ceil(len(ids) / 4)
    top, others = ranked[:top_n], ranked[top_n:]
    def rate(players):
        return 100 * sum(held[p].runs for p in players) / sum(held[p].balls for p in players)
    difference = rate(top) - rate(others)
    rng = np.random.default_rng(2027)
    samples = [rate(rng.choice(top, len(top), replace=True)) - rate(rng.choice(others, len(others), replace=True)) for _ in range(1000)]
    low, high = np.quantile(samples, [0.025, 0.975])
    training = np.array([training_ratings[p] for p in ids])
    observed = np.array([100 * held[p].runs / held[p].balls for p in ids])
    correlation = float(np.corrcoef(training, observed)[0, 1]) if np.std(training) and np.std(observed) else None
    result.update(status="Measured descriptive season holdout", players=len(ids), balls=sum(held[p].balls for p in ids),
                  innings=sum(len(held[p].innings) for p in ids), top_group_players=top_n,
                  top_group_sr=_number(rate(top)), other_group_sr=_number(rate(others)),
                  difference=_number(difference), ci_low=_number(low), ci_high=_number(high), correlation=_number(correlation),
                  description="The highest training-rating quartile is compared with other qualifying players in the last uploaded calendar year. The interval resamples players within each group. This is a descriptive check, not an uncapped auction back-test or a claim of predictive power.")
    return result


def _insight(name, discipline, item):
    if item["status"] != "Available":
        return "Insufficient data"
    if discipline == "batting":
        return (f"{name}: {item['runs']} runs from {item['balls']} balls in {item['innings']} innings. "
                f"Par-adjusted batting rating {item['adjusted_rating']:.2f}; 95% model interval {item['ci_low']:.2f} to {item['ci_high']:.2f}.")
    return (f"{name}: {item['runs']} runs charged in {item['balls']} legal balls across {item['innings']} innings. "
            f"Par-adjusted bowling rating {item['adjusted_rating']:.2f}; 95% model interval {item['ci_low']:.2f} to {item['ci_high']:.2f}.")


def score(root: Path = PROJECT_ROOT):
    """Read the database, precompute all scores and persist their provenance."""
    root = Path(root)
    engine = get_engine(root)
    metadata.create_all(engine)
    players, matches, innings, rows, wickets = _read(engine)
    if not rows:
        raise ValueError("No uploaded regulation T20 deliveries are loaded")
    contexts = _contexts(rows, matches, innings)
    if any(c["rate"] is None for c in contexts.values()):
        raise ValueError("Uploaded context samples are too small for par-adjusted scoring")
    dismissals, credited = _wicket_counts(wickets, innings, rows)
    total, phases, seasons, league_year = _aggregate(rows, contexts, dismissals, credited)
    comp_counts = defaultdict(int)
    for m in matches.values():
        comp_counts[m["competition"]] += 1
    reference = max(sorted(comp_counts), key=comp_counts.get)
    factors, bridges = _league_adjustment(league_year, sorted(comp_counts), reference)
    # League adjustment is only used where the published bridge sample is met.
    total, phases, seasons, _ = _aggregate(rows, contexts, dismissals, credited, factors)
    priors = {d: empirical_prior(g for (p, kind), g in total.items() if kind == d) for d in ("batting", "bowling")}
    phase_priors = {(d, phase): empirical_prior(g for (p, kind, ph), g in phases.items() if kind == d and ph == phase)
                    for d in ("batting", "bowling") for phase, _ in PHASES}
    score_rows = []
    metric_rows = []
    feature_rows, eligible_ids = [], []
    any_bridge = any(b["factor"] is not None for b in bridges)
    adjustment_label = "Par-adjusted descriptive score" + ("; bridge adjustment where sample permits" if any_bridge else "; insufficient bridge players for league transfer")
    for player in players:
        pid, name = player["player_id"], player["canonical_name"]
        bat, bowl = total[(pid, "batting")], total[(pid, "bowling")]
        overall = {"batting": metric(bat, "batting", BATTING_MIN, priors["batting"]),
                   "bowling": metric(bowl, "bowling", BOWLING_MIN, priors["bowling"])}
        phase_items, role_items = [], []
        for phase, label in PHASES:
            split = {d: metric(phases[(pid, d, phase)], d, PHASE_MIN, phase_priors[(d, phase)]) for d in ("batting", "bowling")}
            phase_items.append({"phase": label, "phase_key": phase, **split})
            for d, roles in (("batting", BAT_ROLES), ("bowling", BOWL_ROLES)):
                m = split[d]
                role_items.append({"role": roles[phase], "score": m["adjusted_rating"], "ci_low": m["ci_low"], "ci_high": m["ci_high"],
                                   "sample_balls": m["balls"], "sample_innings": m["innings"], "status": m["status"]})
        available = [d for d in ("batting", "bowling") if overall[d]["status"] == "Available"]
        if len(available) == 2:
            primary_role = "all_rounder"
            rating = sum(overall[d]["adjusted_rating"] for d in available) / 2
            low = sum(overall[d]["ci_low"] for d in available) / 2
            high = sum(overall[d]["ci_high"] for d in available) / 2
            sample_balls = bat.balls + bowl.balls
            sample_innings = len(bat.innings | bowl.innings)
            role_items.append({"role": primary_role, "score": _number(rating), "ci_low": _number(low), "ci_high": _number(high),
                               "sample_balls": sample_balls, "sample_innings": sample_innings, "status": "Available"})
        elif available:
            d = available[0]
            dominant = max(PHASES, key=lambda ph: phases[(pid, d, ph[0])].balls)[0]
            primary_role = (BAT_ROLES if d == "batting" else BOWL_ROLES)[dominant]
            rating, low, high = [overall[d][key] for key in ("adjusted_rating", "ci_low", "ci_high")]
            sample_balls, sample_innings = total[(pid, d)].balls, len(total[(pid, d)].innings)
        else:
            primary_role = "Insufficient data"
            rating = low = high = None
            sample_balls, sample_innings = bat.balls + bowl.balls, len(bat.innings | bowl.innings)
        season_items = []
        for comp, season in sorted({(c, s) for p, d, c, s in seasons if p == pid}):
            entry = {"competition": comp, "season": season}
            for d, minimum in (("batting", BATTING_MIN), ("bowling", BOWLING_MIN)):
                g = seasons[(pid, d, comp, season)]
                entry[d] = metric(g, d, minimum, priors[d])
                metric_rows.append({"player_id": pid, "discipline": d, "competition": comp, "season": season,
                                    "phase": "overall", "metric_json": _dump(entry[d])})
            season_items.append(entry)
        summary = {**overall, "phases": phase_items, "seasons": season_items, "roles": role_items,
                   "insights": [_insight(name, d, overall[d]) for d in ("batting", "bowling")]}
        score_rows.append({"player_id": pid, "primary_role": primary_role, "score": _number(rating), "ci_low": _number(low), "ci_high": _number(high),
                           "rank": None, "sample_balls": sample_balls, "sample_innings": sample_innings,
                           "batting_balls": bat.balls, "bowling_balls": bowl.balls, "archetype": None,
                           "adjustment_label": adjustment_label, "summary_json": _dump(summary)})
        if available:
            eligible_ids.append(pid)
            feature_rows.append([bat.balls / max(bat.balls + bowl.balls, 1),
                                 *[phases[(pid, "batting", ph)].balls / max(bat.balls, 1) for ph, _ in PHASES],
                                 *[phases[(pid, "bowling", ph)].balls / max(bowl.balls, 1) for ph, _ in PHASES]])
    labels, centers = kmeans_numpy(feature_rows)
    clusters = {p: f"Archetype {int(label) + 1}" for p, label in zip(eligible_ids, labels)}
    ranked = sorted([r for r in score_rows if r["score"] is not None], key=lambda r: (-r["score"], r["player_id"]))
    for rank, row in enumerate(ranked, 1):
        row["rank"] = rank
        row["archetype"] = clusters[row["player_id"]]
    par_rows = []
    for (comp, season, venue, phase, d), ctx in sorted(contexts.items()):
        g = ctx["aggregate"]
        shape = g.runs
        low, high = gamma.ppf([0.025, 0.975], shape, scale=1 / g.balls) if shape and g.balls else (None, None)
        par_rows.append({"competition": comp, "season": season, "venue": venue, "phase": phase, "discipline": d,
                         "sample_balls": g.balls, "sample_innings": len(g.innings), "run_rate": _number(ctx["rate"]),
                         "ci_low": _number(low), "ci_high": _number(high), "fallback": ctx["fallback"]})
    venue_par = _venue_par(matches, innings)
    backtest = _holdout(rows, matches, reference)
    methodology = {
        "title": "Methodology",
        "sections": [
            {"title": "Uploaded coverage", "text": "Metrics describe only the uploaded match subset. They are not complete competition records. Player names and IDs are read from match registries. Missing nationality, batting hand and bowling style are not inferred."},
            {"title": "Counting rules", "text": "Wides and no-balls contribute neither balls faced nor legal balls bowled. Bat runs on no-balls remain in the run numerator. Byes, leg-byes and penalties are excluded from bowler runs. Run-outs are credited to the dismissed batter, including non-strikers. Super overs are stored and excluded from metrics."},
            {"title": "Phases and context", "text": "The source numbers overs from zero. Fixed bands cover overs 1 to 6, 7 to 15 and 16 onward. These are fixed bands rather than inferred actual powerplays. Method-marked, shortened and no-result matches are excluded from context baselines; their observed regulation deliveries remain in player rate metrics. Baselines fall back from venue-season-phase to competition-season-phase, competition-phase and competition overall."},
            {"title": "Par adjustment", "text": "Each run contribution is divided by the observed context run rate for its discipline. Batting uses bat runs per ball faced; bowling uses charged runs per legal ball. First-innings par totals use completed, scheduled full-length innings and display a t interval only when the venue-season sample meets the minimum."},
            {"title": "Shrinkage and intervals", "text": "Gamma-Poisson empirical-Bayes priors are estimated from uploaded player rates after subtracting estimated sampling variance. Prior exposure is capped at the batting minimum. Scores compare adjusted rates with context: batting is proportional to scoring rate; bowling is inverse charged run rate. A score of 100 represents the context run rate before shrinkage. Scores use run rate only; dismissals and wickets are displayed separately. Intervals are 95% model intervals under the Gamma-Poisson approximation, not calibrated frequentist coverage guarantees. They ignore opposition strength, within-innings dependence and uncertainty in fitted context and league factors."},
            {"title": "Roles and archetypes", "text": "Primary roles use the phase with the largest observed ball exposure. Players meeting both overall discipline thresholds receive an equal-weight batting and bowling score. Its interval is the average of the component endpoints and does not model dependence. K-means uses standardized observed phase and discipline shares, with no imputed ratings. Cluster numbers describe usage groups rather than quality. Ratings compare scoring or charged run rate within context; a rank across roles is a descriptive ordering rather than a claim of interchangeability. Spin, pace and wicketkeeping roles require missing player attributes."},
            {"title": "League bridges", "text": "Bridge comparisons require the same registered player in both competitions in the same calendar year, meeting the discipline minimum in each. Mean log context-rate differences are averaged by player, with a t interval. Transfer is applied only when the bridge-player minimum is met. Sparse bridge samples retain par-adjusted metrics and are labelled as such."},
            {"title": "Back-test limits", "text": backtest["auction_test_reason"] + " The separate season holdout uses only earlier records for its training rating. It reports the last uploaded year as observed and makes no claim of predictive power."},
        ],
        "thresholds": {"batting_balls": BATTING_MIN, "bowling_balls": BOWLING_MIN, "phase_balls": PHASE_MIN,
                       "par_innings": PAR_MIN_INNINGS, "par_balls": PAR_MIN_BALLS, "bridge_players": BRIDGE_MIN,
                       "holdout_balls": 60, "holdout_players": HOLDOUT_MIN_PLAYERS, "confidence_level": 0.95, "maximum_archetypes": 4},
        "priors": priors, "league_adjustment": bridges, "backtest": backtest,
        "limitations": ["Auction eligibility and price data are absent.", "Squad file not loaded.",
                        "Overseas status, batting hand, bowling style and age are not in uploaded data.",
                        "No independent scorecards are available for a second-source reconciliation.",
                        "Historical ratings describe the uploaded sample. They do not establish current availability or predict future performance."],
    }
    summary = {"players": len(players), "ranked_players": len(ranked), "regulation_deliveries": len(rows),
               "reference_competition": reference, "archetypes": len(centers), "adjustment_label": adjustment_label,
               "league_adjustment": bridges, "backtest": backtest, "venue_par": venue_par}
    with engine.begin() as conn:
        conn.execute(delete(player_metrics)); conn.execute(delete(player_scores)); conn.execute(delete(context_par))
        if score_rows:
            conn.execute(player_scores.insert(), score_rows)
        if metric_rows:
            conn.execute(player_metrics.insert(), metric_rows)
        if par_rows:
            conn.execute(context_par.insert(), par_rows)
        for key, value in (("methodology", methodology), ("model_summary", summary)):
            conn.execute(delete(TABLES["pipeline_meta"]).where(TABLES["pipeline_meta"].c.key == key))
            conn.execute(TABLES["pipeline_meta"].insert().values(key=key, value=_dump(value)))
    report_dir = root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "model_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    return summary
