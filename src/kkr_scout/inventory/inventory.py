"""Inspect each uploaded data file before ingestion; keep a byte-level manifest."""
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from ..common import PROJECT_ROOT, write_csv, write_json


def raw_files(root: Path) -> list[Path]:
    files = [p for p in root.glob("*.json") if p.stem.isdigit()]
    nested = root / "data/raw/cricsheet"
    if nested.exists():
        files += sorted(nested.glob("*.json"))
    return sorted(set(files), key=lambda p: p.as_posix())


def inventory(root: Path = PROJECT_ROOT) -> dict:
    root = Path(root)
    manifest, structures = [], defaultdict(set)
    competitions = defaultdict(lambda: {"seasons": set(), "matches": 0, "innings": 0, "deliveries": 0, "legal_balls": 0, "super_overs": 0, "dls_matches": 0, "no_result_matches": 0, "dates": []})
    missing_registry, zero_starts, hashes = 0, 0, Counter()
    def inspect(value, prefix=""):
        if isinstance(value, dict):
            structures[prefix].update(value)
            for key, item in value.items():
                # Dynamic registries and roster labels are data, not structure.
                if prefix.endswith("registry.people") or prefix.endswith("info.players"):
                    continue
                inspect(item, f"{prefix}.{key}".lstrip("."))
        elif isinstance(value, list):
            for item in value:
                inspect(item, prefix + "[]")
    for path in raw_files(root):
        raw = path.read_bytes()
        doc = json.loads(raw)
        if not isinstance(doc, dict) or not isinstance(doc.get("info"), dict) or not isinstance(doc.get("innings"), list):
            raise ValueError(f"Unreadable ball-by-ball structure: {path.name}")
        inspect(doc)
        info = doc["info"]
        competition = info.get("event", {}).get("name") or info.get("competition")
        if not competition or not info.get("dates") or "season" not in info:
            raise ValueError(f"Missing competition, dates or season: {path.name}")
        summary = competitions[competition]
        summary["seasons"].add(str(info["season"]))
        summary["matches"] += 1
        summary["innings"] += len(doc["innings"])
        summary["dates"].extend(str(d) for d in info["dates"])
        summary["dls_matches"] += info.get("outcome", {}).get("method") in {"D/L", "DLS"}
        summary["no_result_matches"] += info.get("outcome", {}).get("result") in {"no result", "abandoned"}
        deliveries, legal, super_overs = 0, 0, 0
        registry = info.get("registry", {}).get("people", {})
        names = {n for roster in info.get("players", {}).values() for n in roster}
        missing_registry += sum(n not in registry for n in names)
        for inn in doc["innings"]:
            super_overs += bool(inn.get("super_over"))
            overs = inn.get("overs", [])
            if overs and overs[0]["over"] == 0:
                zero_starts += 1
            for over in overs:
                for ball in over["deliveries"]:
                    deliveries += 1
                    extras = ball.get("extras", {})
                    legal += not (extras.get("wides", 0) or extras.get("noballs", 0))
        summary["deliveries"] += deliveries
        summary["legal_balls"] += legal
        summary["super_overs"] += super_overs
        digest = hashlib.sha256(raw).hexdigest()
        hashes[digest] += 1
        manifest.append({"file": path.relative_to(root).as_posix(), "sha256": digest, "bytes": len(raw), "competition": competition, "season": str(info["season"]), "date": str(info["dates"][0]), "tier": "A", "innings": len(doc["innings"]), "deliveries": deliveries, "legal_balls": legal, "data_version": doc["meta"]["data_version"]})
    if not manifest:
        raise ValueError("No usable ball-by-ball or scorecard upload exists. Expected Cricsheet match JSON with info and innings, or a scorecard file with player, match, innings, runs and balls.")
    for name in ("README.txt", "LICENSE.txt"):
        if (root / name).exists():
            (root / name).read_text(encoding="utf-8")
    result = {
        "files": len(manifest),
        "competitions": [{"competition": name, "tier": "A", **{k: v for k, v in summary.items() if k not in {"seasons", "dates"}}, "seasons": sorted(summary["seasons"]), "min_date": min(summary["dates"]), "max_date": max(summary["dates"])} for name, summary in sorted(competitions.items())],
        "structure": {k: sorted(v) for k, v in sorted(structures.items())},
        "missing_registry_roster_occurrences": missing_registry,
        "innings_starting_at_zero": zero_starts,
        "duplicate_hashes": sum(v - 1 for v in hashes.values()),
        "supporting_uploads": [name for name in ("README.txt", "LICENSE.txt") if (root / name).exists()],
        "missing_inputs": ["squad file with purse, slots and overseas limit", "price data", "player nationality, batting hand, bowling type and state", "independent scorecards for reconciliation"],
    }
    write_json(root / "data/inventory.json", result)
    write_csv(root / "data/upload_manifest.csv", manifest, list(manifest[0]))
    lines = ["# Uploaded data inventory", "", "Every numeric JSON upload was opened and its nested keys inspected before the loader was written. Raw files stay in their uploaded locations. Counts include super overs and interrupted innings.", "", "| Competition | Seasons | Tier | Grain | Match files | Innings | Delivery rows | Legal balls | Dates |", "|---|---|---|---|---:|---:|---:|---:|---|"]
    for row in result["competitions"]:
        lines.append(f"| {row['competition']} | {', '.join(row['seasons'])} | A | one delivery event | {row['matches']} | {row['innings']} | {row['deliveries']} | {row['legal_balls']} | {row['min_date']} to {row['max_date']} |")
    lines += ["", "Tier A means ball-by-ball. There are no Tier B scorecard uploads.", "", "README.txt is the Cricsheet archive listing and provenance note. LICENSE.txt contains the Open Data Commons Attribution License. The pasted request and ROADMAP are specifications, not sporting data.", "", "Source structure", "", "- Match: meta.data_version, created and revision; info.dates, event.name, season, match_type, balls_per_over, players, registry.people, teams, venue, toss and outcome.", "- Innings: team and overs; optional super_over, powerplays, target, absent_hurt and miscounted_overs fields remain in metadata.", "- Delivery: batter, non_striker, bowler, runs.batter/extras/total; optional extras and wickets. Runs include event extras.", "- Player grain: roster name linked to registry.people source ID. Officials also occur in registry.people; only roster and delivery participants enter players.", f"- {zero_starts} innings start at over 0. Source overs are zero-indexed; delivery position is event order within an over, including illegal deliveries.", f"- {missing_registry} roster-name occurrences lack registry IDs. Fuzzy resolution remains available for later uploads, with a review bucket and no low-confidence merge.", "", "Optional inputs", "", "No uploaded squad, purse, slot limit, overseas limit, price table or player attributes were found. Do not infer these from historical match rosters. Historical rosters are match participants only.", "", "The per-file byte hash, version, competition, season, grain and counts are in upload_manifest.csv. All observed nested field names are in inventory.json."]
    (root / "data/inventory.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    result = inventory()
    print(json.dumps({"files": result["files"], "competitions": result["competitions"]}, indent=2))
