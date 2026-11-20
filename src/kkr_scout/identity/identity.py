"""Apply manual overrides, source IDs, then cautious contextual name matching."""
import csv
import hashlib
from pathlib import Path

from rapidfuzz.fuzz import WRatio

from ..common import write_csv

FUZZY_THRESHOLD = 97.0
FUZZY_MARGIN = 4.0


class PlayerResolver:
    def __init__(self, overrides: Path | None = None, known: list[dict] | None = None):
        self.overrides, self.name_overrides, self.review = {}, [], []
        self.known = known or []
        if overrides is None or not overrides.exists():
            return
        with overrides.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                source = row.get("source_player_id", "").strip().removeprefix("cricsheet:")
                target = row.get("target_player_id", "").strip().removeprefix("cricsheet:")
                if not target:
                    raise ValueError("Manual override needs target_player_id")
                if source:
                    if source in self.overrides:
                        raise ValueError("Manual override source_player_id must be unique")
                    self.overrides[source] = (target, row.get("canonical_name", "").strip())
                elif row.get("source_name", "").strip():
                    self.name_overrides.append(row)
                else:
                    raise ValueError("Manual override needs source_player_id or contextual source_name")
        for source, (target, _) in self.overrides.items():
            if source != target and target in self.overrides:
                raise ValueError("Override chains are unsupported; map each source directly to its final ID")
        self.known = [{**row, "player_id": "cricsheet:" + self.overrides.get(row["player_id"].removeprefix("cricsheet:"), (row["player_id"].removeprefix("cricsheet:"), ""))[0]} for row in self.known]

    def resolve(self, name: str, registry: dict, competition: str, team: str, season="", state="") -> tuple[str, str | None, str | None]:
        source_id = registry.get(name)
        for row in self.name_overrides:
            if row["source_name"].strip() != name:
                continue
            if all(not row.get(k, "").strip() or row[k].strip() == str(v) for k, v in (("competition", competition), ("team", team), ("season", season), ("state", state))):
                return "cricsheet:" + row["target_player_id"].strip().removeprefix("cricsheet:"), source_id, row.get("canonical_name", "").strip() or None
        if source_id:
            target, canonical = self.overrides.get(source_id, (source_id, ""))
            return f"cricsheet:{target}", source_id, canonical or None
        contextual = [r for r in self.known if r["team"] == team and str(r["season"]) == str(season) and (not state or r.get("state") == state)]
        candidates = {}
        for row in contextual:
            score = WRatio(name, row["name"])
            if score > candidates.get(row["player_id"], (0, None))[0]:
                candidates[row["player_id"]] = (score, row["name"])
        ranked = sorted(((s, pid, n) for pid, (s, n) in candidates.items()), reverse=True)
        best = ranked[0] if ranked else (0, "", "")
        second = ranked[1][0] if len(ranked) > 1 else 0
        if best[0] >= FUZZY_THRESHOLD and best[0] - second >= FUZZY_MARGIN:
            return best[1], None, None
        key = "\0".join((competition, team, str(season), name))
        provisional = "provisional:" + hashlib.sha256(key.encode()).hexdigest()[:20]
        review = {"source_name": name, "competition": competition, "team": team, "season": str(season), "provisional_id": provisional, "candidate_id": best[1], "candidate_name": best[2], "similarity": round(best[0], 3), "margin": round(best[0] - second, 3), "reason": "No contextual candidate" if not ranked else "Below threshold or ambiguous"}
        if review not in self.review:
            self.review.append(review)
        return provisional, None, None

    def write_review(self, path):
        fields = ["source_name", "competition", "team", "season", "provisional_id", "candidate_id", "candidate_name", "similarity", "margin", "reason"]
        write_csv(Path(path), self.review, fields)
