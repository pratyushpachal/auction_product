"""Trace every EDA number to uploaded deliveries and exercise a real small sample."""
import json
import math

import pytest
from scipy.stats import t
from sqlalchemy import inspect, text

from kkr_scout.analysis import EDA_MIN_INNINGS, analyze
from kkr_scout.common import PROJECT_ROOT
from kkr_scout.database import get_engine
from kkr_scout.ingest import ingest
from kkr_scout.inventory import raw_files


def test_persisted_phase_findings_match_independent_sql_and_suppress_small_samples(tmp_path, monkeypatch):
    engine = get_engine()
    if not inspect(engine).has_table("pipeline_meta"):
        ingest(engine=engine)
    with engine.connect() as conn:
        saved = conn.execute(text("SELECT value FROM pipeline_meta WHERE key='eda'")).scalar_one_or_none()
    if saved is None:
        analyze()
        with engine.connect() as conn:
            saved = conn.execute(text("SELECT value FROM pipeline_meta WHERE key='eda'")).scalar_one()
    eda = json.loads(saved)
    # Derive phases from source over numbers and legal flags from source extras,
    # independently of the precomputed over_band and is_legal fields.
    query = text('''WITH observed AS (
        SELECT competition,
          CASE WHEN "over"<6 THEN 'Overs 1 to 6'
               WHEN "over"<15 THEN 'Overs 7 to 15'
               ELSE 'Overs 16 onward' END phase,
          match_id,innings_no,
          SUM(CASE WHEN wides=0 AND noballs=0 THEN 1 ELSE 0 END) balls,
          SUM(total_runs) runs,MAX(balls_per_over) balls_per_over
        FROM regulation_deliveries
        GROUP BY competition,phase,match_id,innings_no
      ), rates AS (
        SELECT competition,phase,balls,
          1.0*runs*balls_per_over/balls rate FROM observed WHERE balls>0
      )
      SELECT competition,phase,COUNT(*) innings,SUM(balls) balls,
        AVG(rate) mean,SUM(rate) rate_sum,SUM(rate*rate) squared_sum
      FROM rates GROUP BY competition,phase''')
    with engine.connect() as conn:
        expected = {(r["competition"], r["phase"]): dict(r) for r in conn.execute(query).mappings()}
    assert len(eda["findings"]) == len(expected)
    assert eda["minimum_innings"] == EDA_MIN_INNINGS
    for finding in eda["findings"]:
        sql = expected[(finding["competition"], finding["phase"])]
        assert finding["innings"] == sql["innings"]
        assert finding["balls"] == sql["balls"]
        assert finding["mean"] == pytest.approx(sql["mean"], abs=1e-10)
        if sql["innings"] >= eda["minimum_innings"]:
            n = sql["innings"]
            variance = max((sql["squared_sum"] - sql["rate_sum"] ** 2 / n) / (n - 1), 0)
            margin = t.ppf(.975, n - 1) * math.sqrt(variance / n)
            assert finding["ci_low"] == pytest.approx(sql["mean"] - margin, abs=1e-9)
            assert finding["ci_high"] == pytest.approx(sql["mean"] + margin, abs=1e-9)
            assert finding["ci_low"] <= finding["mean"] <= finding["ci_high"]
            assert str(sql["balls"]) in finding["insight"]
            assert str(sql["innings"]) in finding["insight"]
            assert "95% confidence interval" in finding["insight"]
        else:
            assert finding["ci_low"] is None and finding["ci_high"] is None
            assert finding["insight"] == "Insufficient data"

    # One untouched uploaded match supplies a genuine below-minimum sample.
    # The production database and reports are not changed by this branch.
    upload = raw_files(PROJECT_ROOT)[0]
    (tmp_path / upload.name).write_bytes(upload.read_bytes())
    test_db = tmp_path / "data/processed/kkr_scout.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{test_db.as_posix()}")
    ingest(root=tmp_path)
    small = analyze(root=tmp_path)
    assert small["findings"]
    for finding in small["findings"]:
        assert 0 < finding["innings"] < small["minimum_innings"]
        assert finding["balls"] > 0
        assert finding["ci_low"] is None and finding["ci_high"] is None
        assert finding["insight"] == "Insufficient data"
