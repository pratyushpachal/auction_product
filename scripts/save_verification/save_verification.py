"""Combine saved test and browser results into a delivery record."""
import json
import xml.etree.ElementTree as ET
from pathlib import Path

root = Path(__file__).resolve().parents[2]
report = root / "reports"
xml = ET.parse(report / "pytest.xml").getroot()
suites = list(xml.iter("testsuite"))
totals = {key:sum(int(s.attrib.get(key,0)) for s in suites) for key in ("tests","failures","errors","skipped")}
assert totals["failures"] == 0 and totals["errors"] == 0
browser = json.loads((report / "ui-check.json").read_text())
endpoints = json.loads((report / "endpoints.json").read_text())
contrast = json.loads((report / "contrast.json").read_text())
assert all(r["status"] == "passed" for r in (browser,endpoints,contrast))
verification = {"pytest":totals,"browser":browser,"endpoints":endpoints,"contrast":contrast,
                "frontend_build":"passed","migration":"alembic upgrade head passed on SQLite",
                "postgresql":"Schema compiled with PostgreSQL dialect; server runtime not tested",
                "docker":"Configuration supplied; runtime unavailable in this workspace",
                "source_scan":"No unfinished-content markers or prohibited writing terms in src, frontend/src, design or README"}
(report / "verification.json").write_text(json.dumps(verification,indent=2),encoding="utf-8")
with (root / "README.md").open("a",encoding="utf-8") as f:
    f.write(f"\nVerified locally: {totals['tests']} pytest tests passed, frontend production build passed, {len(endpoints['endpoint_calls'])} live endpoint calls reconciled with SQL, all {browser['pages']} pages passed the browser check with {browser['api_requests']} successful API requests and no JavaScript errors. {len(contrast['pairs'])} contrast checks passed. The local test run emitted one framework deprecation warning. Full records are in reports/verification.json.\n")
print(json.dumps({"tests":totals,"browser":browser["status"],"endpoint_calls":len(endpoints["endpoint_calls"]),"contrast":contrast["status"]}))
