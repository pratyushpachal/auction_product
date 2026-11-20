"""Run the project from a checkout without installing any packages."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from kkr_scout.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
