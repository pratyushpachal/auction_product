"""Integer selection using only precomputed uploaded-data values."""
import math
import json
import subprocess
import sys
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..service import filter_players, player_data, public, read_meta, rows


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slots: int = Field(ge=1, le=100)
    min_batting: int = Field(default=0, ge=0, le=100)
    min_bowling: int = Field(default=0, ge=0, le=100)
    competition: str | None = None
    season: str | None = None
    excluded_ids: list[str] = Field(default_factory=list, max_length=1000)
    purse: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    overseas_slots: int | None = Field(default=None, ge=0, le=100)
    role_needs: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_needs(self):
        if self.min_batting > self.slots or self.min_bowling > self.slots:
            raise ValueError("A discipline minimum cannot exceed slots")
        if any(v < 0 or v > self.slots for v in self.role_needs.values()):
            raise ValueError("Role minima must be between zero and slots")
        return self


def optimize(conn, request: OptimizeRequest):
    inputs = read_meta(conn, "inputs", {})
    state = inputs.get("constraints", {})
    purse = request.purse if request.purse is not None else state.get("purse")
    overseas_slots = request.overseas_slots if request.overseas_slots is not None else state.get("overseas_slots")
    slots = request.slots
    if state.get("slots") is not None:
        slots = min(slots, int(state["slots"]))
    price_map = {r["player_id"]: r["price"] for r in rows(conn, "SELECT * FROM auction_prices")}
    squad = {r["player_id"]: r for r in rows(conn, "SELECT * FROM squads")}
    if purse is not None and not price_map:
        raise ValueError("Price data not provided. Purse constraints cannot be evaluated.")
    candidates = filter_players(player_data(conn), competition=request.competition, season=request.season)
    known_ids = {p["id"] for p in player_data(conn)}
    if not set(request.excluded_ids) <= known_ids:
        raise ValueError("Excluded player ID is not in uploaded data")
    candidates = [p for p in candidates if p["score"] is not None and p["id"] not in squad and p["id"] not in request.excluded_ids]
    if purse is not None:
        candidates = [p for p in candidates if p["id"] in price_map]
    if overseas_slots is not None:
        # Nationality alone does not establish an auction's domestic/overseas status.
        raise ValueError("Candidate overseas eligibility is not in uploaded data. Overseas constraints cannot be evaluated.")
    candidates.sort(key=lambda p: p["id"])
    roles = {p["role"] for p in candidates}
    if any(role not in roles for role in request.role_needs):
        raise ValueError("Requested role is not in the eligible player data")
    methodological = read_meta(conn, "methodology", {})
    thresholds = methodological.get("thresholds", {})
    batting_min = thresholds.get("batting_balls")
    bowling_min = thresholds.get("bowling_balls")
    if batting_min is None or bowling_min is None:
        raise ValueError("Score thresholds are not prepared. Run the scoring batch before optimization.")
    n = len(candidates)
    base = {"price_message": "Uploaded price data" if price_map else "Price data not provided",
        "constraints_note": "Overseas eligibility is not in uploaded data. Scores describe observed performance; auction availability is unknown.",
        "candidate_count": n, "slots": slots, "min_batting": request.min_batting, "min_bowling": request.min_bowling}
    if not n:
        return {**base, "status": "infeasible", "message": "No eligible players match these constraints.", "players": [], "alternatives": [], "total_value": None, "selected_count": 0, "total_cost": None}
    matrix = [np.ones(n), np.array([p["batting_balls"] >= batting_min for p in candidates], float),
              np.array([p["bowling_balls"] >= bowling_min for p in candidates], float)]
    lower = [0, request.min_batting, request.min_bowling]
    upper = [slots, np.inf, np.inf]
    for role, count in request.role_needs.items():
        matrix.append(np.array([p["role"] == role for p in candidates], float))
        lower.append(count)
        upper.append(np.inf)
    if purse is not None:
        matrix.append(np.array([price_map[p["id"]] for p in candidates]))
        lower.append(0)
        upper.append(purse)
    payload = {"c": [-p["score"] for p in candidates], "matrix": np.stack(matrix).tolist(),
               "lower": [v if math.isfinite(v) else None for v in lower],
               "upper": [v if math.isfinite(v) else None for v in upper]}
    try:
        completed = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "milp_worker" / "milp_worker.py")],
                                   input=json.dumps(payload), text=True, capture_output=True, timeout=25, check=True,
                                   creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        result = json.loads(completed.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError):
        result = {"status": 1, "x": None}
    if result["status"] != 0:
        return {**base, "status": "infeasible" if result["status"] == 2 else "unresolved", "message": "No feasible selection satisfies these constraints." if result["status"] == 2 else "The solver did not establish an optimum within its time limit.",
                "players": [], "alternatives": [], "total_value": None, "selected_count": 0, "total_cost": None}
    selected = [p for p, x in zip(candidates, result["x"]) if x > .5]
    selected.sort(key=lambda p: (-p["score"], p["id"]))
    chosen_ids = {p["id"] for p in selected}
    alternatives = sorted([p for p in candidates if p["id"] not in chosen_ids], key=lambda p: (-p["score"], p["id"]))[:slots]
    return {**base, "status": "optimal", "message": "Selected players maximize the sum of precomputed scores under the requested constraints.",
            "players": [public(p) for p in selected], "alternatives": [public(p) for p in alternatives],
            "total_value": float(sum(p["score"] for p in selected)), "selected_count": len(selected),
            "total_cost": sum(price_map[p["id"]] for p in selected) if purse is not None else None}
