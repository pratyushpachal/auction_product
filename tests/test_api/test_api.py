"""Exercise every data endpoint and compare rendered-data payloads to SQL."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from kkr_scout.common import PROJECT_ROOT
from kkr_scout.database import get_engine


@pytest.fixture(scope="module")
def client():
    from kkr_scout.api import create_app
    from kkr_scout.inputs import load_inputs
    from kkr_scout.cli import prepare
    engine = get_engine()
    ready = False
    if "player_scores" in inspect(engine).get_table_names():
        with engine.connect() as conn:
            ready = bool(conn.execute(text("SELECT COUNT(*) FROM player_scores")).scalar_one())
    if not ready:
        prepare()
    load_inputs()
    with TestClient(create_app()) as result:
        yield result


def test_dashboard_is_exact_sql(client):
    response = client.get("/api/dashboard")
    assert response.status_code == 200
    actual = response.json()
    with get_engine().connect() as conn:
        for key in ("matches", "innings", "deliveries", "players"):
            assert actual["counts"][key] == conn.execute(text(f"SELECT COUNT(*) FROM {key}")).scalar_one()
        assert actual["counts"]["legal_balls"] == conn.execute(text("SELECT SUM(is_legal) FROM deliveries")).scalar_one()
        for row in actual["competitions"]:
            assert row["deliveries"] == conn.execute(text("SELECT COUNT(*) FROM deliveries JOIN matches USING(match_id) WHERE competition=:c"), {"c":row["competition"]}).scalar_one()
        for row in actual["seasons"]:
            assert row["deliveries"] == conn.execute(text("SELECT COUNT(*) FROM deliveries JOIN matches USING(match_id) WHERE season=:s"), {"s":row["season"]}).scalar_one()


def test_search_filters_and_pagination(client):
    data = client.get("/api/players", params={"page_size": 2}).json()
    assert len(data["items"]) == 2
    first = data["items"][0]
    named = client.get("/players", params={"q":first["name"], "sort":"name"}).json()
    assert first["id"] in [p["id"] for p in named["items"]]
    scoped = client.get("/api/players", params={"competition":first["competitions"][0], "role":first["role"], "min_score":first["score"]}).json()
    assert all(p["role"] == first["role"] and p["score"] >= first["score"] for p in scoped["items"])
    assert client.get("/api/players", params={"page":0}).status_code == 422
    assert client.get("/api/players", params={"sort":"invented"}).status_code == 422
    assert client.get("/api/players", params={"min_score":"NaN"}).status_code == 422
    assert client.get("/api/players", params={"q":"' OR 1=1 --"}).json()["total"] == 0


def test_report_scores_samples_and_chart_values_are_sql(client):
    items = client.get("/api/players", params={"page_size": 100}).json()["items"]
    with get_engine().connect() as conn:
        for item in items:
            card = client.get("/api/players/" + item["id"])
            assert card.status_code == 200
            result = card.json()
            row = conn.execute(text("SELECT * FROM player_scores WHERE player_id=:p"), {"p":item["id"]}).mappings().one()
            for key in ("score", "ci_low", "ci_high", "rank", "sample_balls", "sample_innings", "batting_balls", "bowling_balls", "archetype"):
                assert result["player"][key] == row[key]
            assert result["metrics"] == json.loads(row["summary_json"])
            for key in ("state", "batting_hand", "bowling_type", "dob", "nationality"):
                assert result["player"][key] is None
    assert client.get("/players/unknown").status_code == 404


def test_compare_returns_real_reports(client):
    items = client.get("/players", params={"page_size":2}).json()["items"]
    ids = ",".join(p["id"] for p in items)
    response = client.get("/compare", params={"ids":ids})
    assert response.status_code == 200
    assert [r["player"]["id"] for r in response.json()["players"]] == [p["id"] for p in items]
    assert client.get("/compare", params={"ids":items[0]["id"]}).status_code == 422
    assert client.get("/compare", params={"ids":items[0]["id"]+",unknown"}).status_code == 404


def test_missing_squad_and_working_templates(client):
    data = client.get("/squad/gaps").json()
    assert not data["loaded"] and data["message"] == "Squad file not loaded"
    assert data["price_message"] == "Price data not provided"
    for filename in ("squad.csv", "prices.csv", "constraints.csv"):
        result = client.get("/api/templates/" + filename)
        assert result.status_code == 200
        assert ("purse" if filename == "constraints.csv" else "player_id") in result.text
        assert len(result.text.strip().splitlines()) == 1


def test_optimizer_constraints_and_exact_value(client):
    response = client.post("/optimize", json={"slots":3,"min_batting":1,"min_bowling":1})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "optimal" and data["selected_count"] <= 3
    assert data["total_value"] == pytest.approx(sum(p["score"] for p in data["players"]))
    assert any(p["batting_balls"] >= 150 for p in data["players"])
    assert any(p["bowling_balls"] >= 120 for p in data["players"])
    assert data["total_cost"] is None
    excluded = [p["id"] for p in data["players"]]
    alternative = client.post("/api/optimize", json={"slots":3,"excluded_ids":excluded}).json()
    assert not set(excluded) & {p["id"] for p in alternative["players"]}
    empty = client.post("/api/optimize", json={"slots":3,"competition":"not-an-upload"}).json()
    assert empty["status"] == "infeasible" and empty["players"] == []
    assert client.post("/api/optimize", json={"slots":0}).status_code == 422
    assert client.post("/api/optimize", json={"slots":1,"min_batting":2}).status_code == 422
    assert client.post("/api/optimize", json={"slots":1,"purse":100}).status_code == 422
    assert client.post("/api/optimize", json={"slots":1,"overseas_slots":1}).status_code == 422
    assert client.post("/api/optimize", json={"slots":1,"fake_input":1}).status_code == 422


def test_methodology_and_health(client):
    response = client.get("/api/methodology")
    assert response.status_code == 200
    data = response.json()
    assert data["quality"] and data["inventory"]["files"] == client.get("/health").json()["matches"]
    assert data["methodology"]["thresholds"]["batting_balls"] == 150
    assert data["methodology"]["backtest"]
