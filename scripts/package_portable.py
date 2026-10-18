"""Build a relocatable Windows package with data and a standalone Python runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".ruff_cache")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build(output: Path) -> Path:
    if sys.platform != "win32":
        raise RuntimeError("This builder requires Windows x64 Python")
    output.mkdir(parents=True, exist_ok=True)
    name = "Hawk-Windows-Portable-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    stage = output / name
    stage.mkdir()
    for folder in ("backend", "config", "scripts", "tests", "docs"):
        shutil.copytree(ROOT / folder, stage / folder, ignore=IGNORE)
    for filename in ("README.md", "requirements.txt", "pyproject.toml", ".gitignore",
                     "Start-Hawk.cmd", "PACKAGE-README.txt"):
        shutil.copy2(ROOT / filename, stage / filename)

    launcher = stage / "Start-Hawk.cmd"
    launcher.write_text(launcher.read_text(encoding="utf-8"),
                        encoding="utf-8", newline="\r\n")

    runtime = stage / "runtime"
    base = Path(sys.base_prefix)
    runtime.mkdir()
    for file in base.iterdir():
        if file.is_file() and (file.suffix.lower() in (".exe", ".dll", ".txt")):
            shutil.copy2(file, runtime / file.name)
    for folder in ("DLLs", "Lib", "tcl"):
        def ignore_runtime(directory, names):
            skip = set(IGNORE(directory, names))
            if Path(directory) == base / "Lib":
                skip.add("site-packages")
            return list(skip)
        shutil.copytree(base / folder, runtime / folder, ignore=ignore_runtime)
    packages = Path(sys.prefix) / "Lib" / "site-packages"
    if packages == base / "Lib" / "site-packages":
        raise RuntimeError("Run this builder from the project's isolated .venv")
    shutil.copytree(packages, runtime / "Lib" / "site-packages", ignore=IGNORE)

    data = stage / "data"
    data.mkdir()
    source_sqlite = sqlite3.connect((ROOT / "data" / "hawk.sqlite").as_uri() + "?mode=ro",
                                    uri=True)
    destination_sqlite = sqlite3.connect(data / "hawk.sqlite")
    try:
        source_sqlite.backup(destination_sqlite)
    finally:
        destination_sqlite.close()
        source_sqlite.close()
    database = ROOT / "data" / "hawk.duckdb"
    wal = database.with_suffix(".duckdb.wal")
    if wal.exists() and wal.stat().st_size:
        raise RuntimeError("DuckDB has uncheckpointed WAL; finish the writer before packaging")
    with duckdb.connect(str(database), read_only=True):
        shutil.copy2(database, data / database.name)
    for folder in ("snapshots", "templates"):
        if (ROOT / "data" / folder).exists():
            shutil.copytree(ROOT / "data" / folder, data / folder, ignore=IGNORE)
    for file in (ROOT / "data").iterdir():
        if file.is_file() and (file.name.startswith("codes_") or
                               file.name.startswith("recommend_") or
                               file.name in ("review_report.json", "portfolio_health.json")):
            shutil.copy2(file, data / file.name)

    files = []
    for path in sorted(stage.rglob("*")):
        if path.is_file():
            files.append({"path": path.relative_to(stage).as_posix(),
                          "size": path.stat().st_size, "sha256": digest(path)})
    (stage / "manifest.json").write_text(
        json.dumps({"package": name, "python": sys.version, "files": files,
                    "excluded": ["secrets.vault.json", "models", "tools", "logs"]},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    archive = output / (name + ".zip")
    print(f"Creating {archive}", flush=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
        for file in sorted(stage.rglob("*")):
            if file.is_file():
                zipped.write(file, file.relative_to(output).as_posix())
    with zipfile.ZipFile(archive) as zipped:
        bad = zipped.testzip()
        if bad:
            raise RuntimeError(f"Archive integrity failure: {bad}")
    (output / (name + ".sha256.txt")).write_text(
        f"{digest(archive)}  {archive.name}\n", encoding="ascii")
    print(json.dumps({"archive": str(archive), "stage": str(stage),
                      "bytes": archive.stat().st_size, "file_count": len(files)}, indent=2))
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "release")
    args = parser.parse_args()
    build(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
