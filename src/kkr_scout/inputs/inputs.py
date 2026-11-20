"""Load optional user-provided squad, constraint and price CSVs without defaults."""
import csv
import json
from pathlib import Path

from sqlalchemy import Boolean, Column, Float, MetaData, String, Table, delete, select

from ..common import PROJECT_ROOT, write_json
from ..database import get_engine, TABLES

inputs_metadata = MetaData()
squads = Table("squads", inputs_metadata,
    Column("player_id", String, primary_key=True), Column("role", String),
    Column("overseas", Boolean), Column("batting_hand", String),
    Column("bowling_type", String), Column("price", Float))
prices = Table("auction_prices", inputs_metadata,
    Column("player_id", String, primary_key=True), Column("price", Float, nullable=False))


def _rows(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _number(value, field):
    if value is None or str(value).strip() == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a finite, non-negative number") from error
    if result < 0 or not __import__("math").isfinite(result):
        raise ValueError(f"{field} must be a finite, non-negative number")
    return result


def load_inputs(root: Path = PROJECT_ROOT):
    engine = get_engine(root)
    inputs_metadata.create_all(engine)
    squad_rows = _rows(root / "data/squad.csv")
    price_rows = _rows(root / "data/prices.csv")
    constraints_rows = _rows(root / "data/constraints.csv")
    if len(constraints_rows) > 1:
        raise ValueError("constraints.csv must have exactly one row")
    constraints = {}
    if constraints_rows:
        for key in ("purse", "slots", "overseas_slots"):
            value = _number(constraints_rows[0].get(key), key)
            if key != "purse" and value is not None and int(value) != value:
                raise ValueError(f"{key} must be an integer")
            constraints[key] = value
        if constraints.get("slots") is None:
            raise ValueError("constraints.csv requires a slots value")
        if constraints.get("overseas_slots") is not None and constraints["overseas_slots"] > constraints["slots"]:
            raise ValueError("overseas_slots cannot exceed total slots")
    with engine.begin() as conn:
        valid_ids = set(conn.execute(select(TABLES["players"].c.player_id)).scalars())
        conn.execute(delete(squads))
        conn.execute(delete(prices))
        seen = set()
        for row in squad_rows:
            pid = row.get("player_id", "").strip()
            if pid not in valid_ids or pid in seen:
                raise ValueError("squad.csv requires unique player_id values from the player database")
            seen.add(pid)
            overseas = row.get("overseas", "").strip().lower()
            if overseas not in ("", "true", "false", "1", "0"):
                raise ValueError("overseas must be true, false, 1, 0 or empty")
            conn.execute(squads.insert().values(player_id=pid, role=(row.get("role") or "").strip() or None,
                overseas=None if not overseas else overseas in ("true", "1"),
                batting_hand=(row.get("batting_hand") or "").strip() or None,
                bowling_type=(row.get("bowling_type") or "").strip() or None, price=_number(row.get("price"), "price")))
        seen = set()
        for row in price_rows:
            pid = row.get("player_id", "").strip()
            price = _number(row.get("price"), "price")
            if pid not in valid_ids or pid in seen or price is None:
                raise ValueError("prices.csv requires unique known player_id values and nonempty prices")
            seen.add(pid)
            conn.execute(prices.insert().values(player_id=pid, price=price))
        meta = TABLES["pipeline_meta"]
        state = {"squad_loaded": bool(squad_rows), "price_loaded": bool(price_rows),
                 "constraints_loaded": bool(constraints_rows), "constraints": constraints}
        conn.execute(delete(meta).where(meta.c.key == "inputs"))
        conn.execute(meta.insert().values(key="inputs", value=json.dumps(state)))
        value = conn.execute(select(meta.c.value).where(meta.c.key == "inventory")).scalar_one_or_none()
        if value:
            inventory = json.loads(value)
            dynamic = {"squad file with purse, slots and overseas limit", "squad file", "price data"}
            missing = [item for item in inventory.get("missing_inputs", []) if item not in dynamic and not item.startswith("Missing squad constraints:")]
            constraint_fields = {"purse": "purse", "slots": "slots", "overseas_slots": "overseas limit"}
            absent_constraints = [label for key, label in constraint_fields.items() if constraints.get(key) is None]
            if not squad_rows and len(absent_constraints) == len(constraint_fields):
                missing.insert(0, "squad file with purse, slots and overseas limit")
            else:
                if not squad_rows:
                    missing.insert(0, "squad file")
                if absent_constraints:
                    missing.insert(0, "Missing squad constraints: " + ", ".join(absent_constraints))
            if not price_rows:
                missing.insert(1 if missing else 0, "price data")
            inventory["missing_inputs"] = missing
            inventory["optional_inputs"] = {"squad_rows": len(squad_rows), "price_rows": len(price_rows), "constraints_loaded": bool(constraints_rows)}
            conn.execute(delete(meta).where(meta.c.key == "inventory"))
            conn.execute(meta.insert().values(key="inventory", value=json.dumps(inventory)))
        else:
            inventory = None
    if inventory:
        write_json(root / "data/inventory.json", inventory)
    return state
