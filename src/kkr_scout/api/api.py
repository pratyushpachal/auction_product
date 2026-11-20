"""Read-only scouting API and built frontend delivery."""
import logging
from collections import Counter
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from ..common import PROJECT_ROOT
from ..database import get_engine
from ..optimizer import OptimizeRequest, optimize
from ..service import filter_players, player_data, public, read_meta, report, rows

logger = logging.getLogger("kkr_scout")


def create_app(root: Path = PROJECT_ROOT):
    app = FastAPI(title="KKR Auction Scout", version="1.0.0", description="Uploaded match data, computed scores and sample limits.")
    engine = get_engine(root)

    def options(items):
        return {"competitions": sorted({v for p in items for v in p["competitions"]}),
                "seasons": sorted({v for p in items for v in p["seasons"]}, reverse=True),
                "roles": sorted({p["role"] for p in items if p["role"]}),
                "archetypes": sorted({p["archetype"] for p in items if p["archetype"]})}

    @app.get("/api/health")
    @app.get("/health")
    def health():
        with engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM matches")).scalar_one()
            scored = conn.execute(text("SELECT COUNT(*) FROM player_scores")).scalar_one()
        return {"status": "ok", "matches": count, "scored_players": scored}

    @app.get("/api/players")
    @app.get("/players")
    def players(q: str | None = Query(None, max_length=100), competition: str | None = None,
                season: str | None = None, role: str | None = None, archetype: str | None = None,
                min_score: float | None = Query(None, ge=0, allow_inf_nan=False),
                sort: Literal["score", "name", "sample"] = "score",
                page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        with engine.connect() as conn:
            all_items = player_data(conn)
        filtered = filter_players(all_items, q, competition, season, role, archetype, min_score)
        if sort == "name":
            filtered.sort(key=lambda p: (p["name"].casefold(), p["id"]))
        elif sort == "sample":
            filtered.sort(key=lambda p: (-(p["sample_balls"] or 0), p["id"]))
        else:
            filtered.sort(key=lambda p: (p["score"] is None, -(p["score"] or 0), p["id"]))
        start = (page - 1) * page_size
        return {"items": [public(p) for p in filtered[start:start+page_size]], "total": len(filtered),
                "page": page, "page_size": page_size, "options": options(all_items),
                "score_scope": "Scores use all uploaded seasons. Competition and season filters select observed participants."}

    @app.get("/api/players/{player_id}")
    @app.get("/players/{player_id}")
    def player(player_id: str):
        with engine.connect() as conn:
            all_items = player_data(conn)
        found = next((p for p in all_items if p["id"] == player_id), None)
        if not found:
            raise HTTPException(404, "Player ID is not in uploaded data")
        return report(found, all_items)

    @app.get("/api/compare")
    @app.get("/compare")
    def compare(ids: str = Query(min_length=1, max_length=1000)):
        requested = ids.split(",")
        if len(requested) < 2 or len(requested) > 4 or len(set(requested)) != len(requested):
            raise HTTPException(422, "Choose two to four different players")
        with engine.connect() as conn:
            all_items = player_data(conn)
        by_id = {p["id"]: p for p in all_items}
        if any(pid not in by_id for pid in requested):
            raise HTTPException(404, "Player ID is not in uploaded data")
        return {"players": [report(by_id[pid], all_items) for pid in requested]}

    @app.get("/api/dashboard")
    def dashboard():
        with engine.connect() as conn:
            counts = {name:conn.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar_one() for name in ("matches", "innings", "deliveries", "players")}
            counts["legal_balls"] = conn.execute(text("SELECT SUM(is_legal) FROM deliveries")).scalar_one()
            counts["ranked_players"] = conn.execute(text("SELECT COUNT(*) FROM player_scores WHERE score IS NOT NULL")).scalar_one()
            competitions = rows(conn, """SELECT m.competition, COUNT(DISTINCT m.match_id) AS matches,
                MIN(m.date) AS min_date, MAX(m.date) AS max_date FROM matches m GROUP BY m.competition ORDER BY m.competition""")
            for competition in competitions:
                competition.update(rows(conn, """SELECT COUNT(*) AS innings FROM innings i JOIN matches m USING(match_id) WHERE m.competition=:competition""", competition)[0])
                competition.update(rows(conn, """SELECT COUNT(*) AS deliveries,SUM(is_legal) AS legal_balls FROM deliveries d JOIN matches m USING(match_id) WHERE m.competition=:competition""", competition)[0])
            seasons = rows(conn, """SELECT m.season, COUNT(DISTINCT m.match_id) AS matches,COUNT(d.delivery_no) AS deliveries
                FROM matches m LEFT JOIN deliveries d USING(match_id) GROUP BY m.season ORDER BY m.season""")
            items = [p for p in player_data(conn) if p["score"] is not None]
            items.sort(key=lambda p: (p["rank"] or 999999, p["id"]))
            inventory = read_meta(conn, "inventory", {})
            team_coverage = rows(conn, """SELECT competition,team,COUNT(*) AS matches FROM (
                SELECT competition,team1 AS team FROM matches UNION ALL
                SELECT competition,team2 AS team FROM matches
                ) teams GROUP BY competition,team ORDER BY competition,matches DESC,team""")
        return {"counts": counts, "competitions": competitions, "seasons": seasons,
                "team_coverage": team_coverage,
                "leaders": [public(p) for p in items[:6]], "missing_inputs": inventory.get("missing_inputs", []),
                "scope_note": "Coverage is limited to the uploaded matches. Scores describe this sample and do not establish auction availability or future performance."}

    @app.get("/api/squad/gaps")
    @app.get("/squad/gaps")
    def squad_gaps():
        with engine.connect() as conn:
            inputs = read_meta(conn, "inputs", {})
            inventory = read_meta(conn, "inventory", {})
            squad = rows(conn, "SELECT * FROM squads")
            items = {p["id"]:p for p in player_data(conn)}
            methodological = read_meta(conn, "methodology", {})
        base = {"loaded": bool(squad), "constraints_loaded": inputs.get("constraints_loaded", False),
                "constraints": inputs.get("constraints", {}), "template_url": "/api/templates/squad.csv",
                "missing_inputs": inventory.get("missing_inputs", []),
                "price_message": "Uploaded price data" if inputs.get("price_loaded") else "Price data not provided"}
        if not squad:
            return {**base, "message": "Squad file not loaded"}
        coverage = Counter()
        archetype_coverage = Counter()
        phase_coverage = Counter()
        for member in squad:
            p = items[member["player_id"]]
            coverage[member["role"] or p["role"] or "Not in uploaded data"] += 1
            archetype_coverage[p["archetype"] or "Insufficient data"] += 1
            for phase in p["_metrics"].get("phases", []):
                for discipline in ("batting", "bowling"):
                    metric = phase.get(discipline) or {}
                    if metric.get("status") == "Available":
                        phase_coverage[(phase["phase"], discipline)] += 1
        return {**base, "message": "Coverage from the uploaded squad file", "players": [public(items[r["player_id"]]) for r in squad],
                "coverage": [{"role":k,"count":v} for k,v in sorted(coverage.items())],
                "archetype_coverage": [{"archetype":k,"count":v} for k,v in sorted(archetype_coverage.items())],
                "phase_coverage": [{"phase":k[0],"discipline":k[1],"count":v} for k,v in sorted(phase_coverage.items())],
                "overseas": None if any(r["overseas"] is None for r in squad) else sum(bool(r["overseas"]) for r in squad),
                "batting_mix": dict(Counter(r["batting_hand"] or "Not in uploaded data" for r in squad)),
                "bowling_mix": dict(Counter(r["bowling_type"] or "Not in uploaded data" for r in squad))}

    @app.post("/api/optimize")
    @app.post("/optimize")
    def optimization(request: OptimizeRequest):
        try:
            with engine.connect() as conn:
                return optimize(conn, request)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/methodology")
    def methodology():
        with engine.connect() as conn:
            return {"inventory": read_meta(conn, "inventory", {}), "methodology": read_meta(conn, "methodology", {}),
                    "model_summary": read_meta(conn, "model_summary", {}), "quality": read_meta(conn, "data_quality", []),
                    "missing_inputs": read_meta(conn, "inventory", {}).get("missing_inputs", []),
                    "design": read_meta(conn, "design", {}), "eda": read_meta(conn, "eda", {})}

    @app.get("/api/templates/{filename}")
    def template(filename: Literal["squad.csv", "prices.csv", "constraints.csv"]):
        path = root / "data" / (filename.removesuffix(".csv") + "_template.csv")
        if not path.exists():
            raise HTTPException(404, "Template file is unavailable")
        return FileResponse(path, media_type="text/csv", filename=filename)

    dist = root / "frontend/dist"
    if (dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/")
    def index():
        if not (dist / "index.html").exists():
            raise HTTPException(503, "Build the frontend with npm run build in frontend before serving")
        return FileResponse(dist / "index.html")

    return app
