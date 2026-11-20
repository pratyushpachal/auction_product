"""Package source, raw uploads and the verified frontend build."""
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[2]
output = root / "dist/kkr-auction-scout.zip"
output.parent.mkdir(exist_ok=True)
excluded = {".venv", "node_modules", "__pycache__", ".pytest_cache", ".git", "dist"}
with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
    for file in sorted(root.rglob("*")):
        relative = file.relative_to(root)
        if not file.is_file() or any(p in excluded for p in relative.parts if relative.parts[:2] != ("frontend", "dist")):
            continue
        if file.name == ".env" or file.suffix in (".pyc", ".sqlite", ".db") or file.name.endswith(("-wal", "-shm")):
            continue
        archive.write(file, "kkr-auction-scout/" + relative.as_posix())
with zipfile.ZipFile(output) as archive:
    assert archive.testzip() is None
    assert "kkr-auction-scout/frontend/dist/index.html" in archive.namelist()
    raw = [p for p in archive.namelist() if Path(p).stem.isdigit() and p.endswith(".json")]
    print(f"Packaged {len(archive.namelist())} files including {len(raw)} original match JSON files: {output} ({output.stat().st_size:,} bytes)")
