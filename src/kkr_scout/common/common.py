"""Small standard-library helpers shared by the pipeline."""
import csv
import hashlib
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_config(root: Path = PROJECT_ROOT) -> dict:
    return json.loads((root / "config/project.json").read_text(encoding="utf-8"))


def canonical_team(name: str) -> str:
    """Keep source team labels; uploads have no team identity mapping."""
    return name


def over_band(over: int) -> str:
    """Fixed T20 over bands; actual powerplays remain in innings metadata."""
    if over < 6:
        return "overs_1_6"
    if over < 15:
        return "overs_7_15"
    return "overs_16_plus"
