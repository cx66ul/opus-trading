"""Build a relocatable runtime from the current CPython and the validated dependency venv.
A venv alone is not portable: pyvenv.cfg points back to the developer's Python.
No downloads are needed when --site-packages points to the existing runtime.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ("duckdb", "fastapi", "uvicorn", "httpx", "yaml", "loguru")

def verify(target: Path) -> dict:
    code = """
import importlib,json,pathlib,sys
root=pathlib.Path(sys.executable).resolve().parent
mods=[importlib.import_module(n) for n in ('encodings','duckdb','fastapi','uvicorn','httpx','yaml','loguru')]
assert pathlib.Path(sys.base_prefix).resolve()==root, (sys.base_prefix,root)
assert all(pathlib.Path(m.__file__).resolve().is_relative_to(root) for m in mods)
print(json.dumps({'python':sys.executable,'base_prefix':sys.base_prefix,'modules':{m.__name__:m.__file__ for m in mods}}))
"""
    env = {k: v for k, v in os.environ.items() if k.upper() not in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV")}
    result = subprocess.run([str(target / "python.exe"), "-I", "-c", code], env=env,
                            cwd=target, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)

def build(target: Path, site: Path) -> dict:
    if target.exists():
        raise SystemExit(f"Refusing to overwrite {target}; use --verify or a new --output.")
    base = Path(sys.base_prefix)
    target.mkdir(parents=True)
    for name in ("python.exe", "pythonw.exe", "LICENSE.txt"):
        shutil.copy2(base / name, target / name)
    for dll in base.glob("*.dll"):
        shutil.copy2(dll, target / dll.name)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "test", "tests", "idlelib", "site-packages")
    shutil.copytree(base / "Lib", target / "Lib", ignore=ignore)
    shutil.copytree(base / "DLLs", target / "DLLs", ignore=ignore)
    shutil.copytree(site, target / "Lib" / "site-packages",
                   ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "pip", "pip-*.dist-info"))
    result = verify(target)
    (target / "runtime-manifest.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime-standalone")
    parser.add_argument("--site-packages", type=Path, default=ROOT / "runtime" / "Lib" / "site-packages")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    print(json.dumps(verify(args.output) if args.verify else build(args.output, args.site_packages), indent=2))
