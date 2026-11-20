"""One command to inventory, ingest, score and serve."""
import argparse
import json
import os
from pathlib import Path

from sqlalchemy import delete

from ..common import PROJECT_ROOT


def prepare(root=PROJECT_ROOT):
    from ..ingest import ingest
    from ..metrics import score
    from ..inputs import load_inputs
    from ..database import get_engine, TABLES
    ingested = ingest(root)
    print(json.dumps({"ingestion": ingested}, ensure_ascii=False))
    scored = score(root)
    print(json.dumps({"model": {k:v for k,v in scored.items() if k != "venue_par"}}, ensure_ascii=False))
    from ..analysis import analyze
    analyze(root)
    optional = load_inputs(root)
    print(json.dumps({"optional_inputs": optional}))
    tokens = root / "design/tokens.json"
    if tokens.exists():
        design = json.loads(tokens.read_text(encoding="utf-8"))
        with get_engine(root).begin() as conn:
            table = TABLES["pipeline_meta"]
            conn.execute(delete(table).where(table.c.key == "design"))
            conn.execute(table.insert().values(key="design", value=json.dumps(design)))
    return {"ingestion": ingested, "model": scored, "optional_inputs": optional}


def main():
    parser = argparse.ArgumentParser(description="Scouting from uploaded match files")
    parser.add_argument("command", nargs="?", default="run", choices=("run", "prepare", "inventory", "ingest", "score", "serve"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()
    if args.command in ("run", "prepare"):
        prepare()
    elif args.command == "inventory":
        from ..inventory import inventory
        result = inventory(PROJECT_ROOT)
        print(json.dumps({"files": result["files"], "competitions": result["competitions"]}, ensure_ascii=False))
    elif args.command == "ingest":
        from ..ingest import ingest
        print(json.dumps(ingest(), ensure_ascii=False))
    elif args.command == "score":
        from ..metrics import score
        print(json.dumps(score(), ensure_ascii=False))
    if args.command in ("run", "serve"):
        import uvicorn
        from ..api import create_app
        uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0
