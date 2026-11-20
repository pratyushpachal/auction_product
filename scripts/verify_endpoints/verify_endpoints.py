"""Call the running server and reconcile response data with SQL."""
import json
import os
import sys
import urllib.request
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "src"))
from sqlalchemy import text
from kkr_scout.database import get_engine

base = os.environ.get("SCOUT_URL", "http://127.0.0.1:8000")
checks = []


def request(path, body=None, json_response=True):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base+path, data=data, headers={"Content-Type":"application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=40) as result:
        payload = result.read()
        checks.append({"method":"POST" if body is not None else "GET","path":path,"status":result.status})
    return json.loads(payload) if json_response else payload


request("/", json_response=False)
request("/docs", json_response=False)
request("/openapi.json")
request("/health")
request("/api/health")
dashboard = request("/api/dashboard")
players = request("/players?page_size=2")["items"]
request("/api/players?page_size=2")
for p in players:
    card = request("/players/"+p["id"])
    request("/api/players/"+p["id"])
    with get_engine().connect() as conn:
        stored = conn.execute(text("SELECT * FROM player_scores WHERE player_id=:id"), {"id":p["id"]}).mappings().one()
        assert card["metrics"] == json.loads(stored["summary_json"])
        assert all(card["player"][k] == stored[k] for k in ("score","ci_low","ci_high","rank","sample_balls","sample_innings"))
ids = ",".join(p["id"] for p in players)
request("/compare?ids="+ids)
request("/api/compare?ids="+ids)
request("/squad/gaps")
request("/api/squad/gaps")
result = request("/optimize", {"slots":2,"min_batting":1,"min_bowling":1})
assert result["status"] == "optimal"
assert result["total_value"] == sum(p["score"] for p in result["players"])
request("/api/optimize", {"slots":2})
methodology = request("/api/methodology")
for name in ("squad.csv","prices.csv","constraints.csv"):
    request("/api/templates/"+name, json_response=False)
with get_engine().connect() as conn:
    for name in ("matches","innings","deliveries","players"):
        assert dashboard["counts"][name] == conn.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar_one()
    assert dashboard["counts"]["legal_balls"] == conn.execute(text("SELECT SUM(is_legal) FROM deliveries")).scalar_one()
    for key in ("inventory","methodology","model_summary","data_quality","eda","design"):
        source = conn.execute(text("SELECT value FROM pipeline_meta WHERE key=:key"), {"key":key}).scalar_one()
        assert methodology["quality" if key == "data_quality" else key] == json.loads(source)
result = {"status":"passed","endpoint_calls":checks,"sql_reconciliation":"Dashboard counts, report/chart payloads, score intervals, optimizer sums and methodology metadata match SQL."}
(root / "reports/endpoints.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps({"status":"passed","endpoint_calls":len(checks),"sql_reconciliation":"passed"}))
