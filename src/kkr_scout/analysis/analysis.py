"""Six fixed-template EDA findings with innings-level t intervals."""
import json
from collections import defaultdict

import numpy as np
from scipy.stats import t
from sqlalchemy import delete, text

from ..common import PROJECT_ROOT
from ..database import TABLES, get_engine

EDA_MIN_INNINGS = 10


def analyze(root=PROJECT_ROOT):
    engine = get_engine(root)
    with engine.connect() as conn:
        observations = conn.execute(text("""SELECT m.competition,d.over_band,d.match_id,d.innings_no,
            SUM(d.total_runs) AS runs,SUM(d.is_legal) AS balls FROM deliveries d
            JOIN innings i USING(match_id,innings_no) JOIN matches m USING(match_id)
            WHERE i.is_super_over=0 GROUP BY m.competition,d.over_band,d.match_id,d.innings_no""")).mappings().all()
    grouped = defaultdict(list)
    for row in observations:
        if row["balls"]:
            grouped[(row["competition"], row["over_band"])].append(row)
    findings = []
    labels = {"overs_1_6": "Overs 1 to 6", "overs_7_15": "Overs 7 to 15", "overs_16_plus": "Overs 16 onward"}
    for (competition, phase), rows in sorted(grouped.items()):
        rates = np.array([r["runs"] * 6 / r["balls"] for r in rows])
        balls = sum(r["balls"] for r in rows)
        n = len(rows)
        mean = float(rates.mean())
        lo = hi = None
        if n >= EDA_MIN_INNINGS:
            margin = float(t.ppf(.975, n - 1) * rates.std(ddof=1) / np.sqrt(n))
            lo, hi = mean - margin, mean + margin
            insight = f"{competition}, {labels[phase]}: mean innings scoring rate {mean:.2f} runs per six legal balls, with 95% confidence interval {lo:.2f} to {hi:.2f}, from {n} innings and {balls} legal balls."
        else:
            insight = "Insufficient data"
        findings.append({"competition": competition, "phase": labels[phase], "mean": mean,
                         "ci_low": lo, "ci_high": hi, "innings": n, "balls": balls, "insight": insight})
    result = {"minimum_innings": EDA_MIN_INNINGS, "findings": findings,
              "method": "Unweighted mean of each innings-phase total run rate. A 95% t interval treats innings as independent. It describes this uploaded subset; interrupted regulation innings remain included."}
    with engine.begin() as conn:
        meta = TABLES["pipeline_meta"]
        conn.execute(delete(meta).where(meta.c.key == "eda"))
        conn.execute(meta.insert().values(key="eda", value=json.dumps(result)))
    path = root / "reports"
    path.mkdir(exist_ok=True)
    (path / "eda.md").write_text("# Uploaded-data analysis\n\n" + result["method"] + "\n\n" + "\n\n".join(f["insight"] for f in findings) + "\n", encoding="utf-8")
    return result
