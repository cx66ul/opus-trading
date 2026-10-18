"""Extract a release into a new directory and verify hashes and runtime relocation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import uuid
import zipfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    archive = args.archive.resolve()
    target = archive.parent / ("verify-" + uuid.uuid4().hex[:8])
    target.mkdir()
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.namelist():
            destination = (target / member).resolve()
            if not destination.is_relative_to(target):
                raise ValueError("Unsafe archive entry")
        zipped.extractall(target)
    folders = [p for p in target.iterdir() if p.is_dir()]
    if len(folders) != 1:
        raise RuntimeError("Expected exactly one package root")
    root = folders[0]
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest["files"]:
        file = root / item["path"]
        with file.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != item["sha256"] or file.stat().st_size != item["size"]:
            raise RuntimeError(f"File integrity failure: {item['path']}")
    if (root / "data" / "secrets.vault.json").exists():
        raise RuntimeError("Credential vault must not be distributed")
    python = root / "runtime" / "python.exe"
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    launcher = subprocess.run(["cmd.exe", "/d", "/c", "Start-Hawk.cmd"],
                              cwd=root, env=environment, input=b"0\r\n",
                              capture_output=True, timeout=20)
    launcher_output = (launcher.stdout + launcher.stderr).decode("utf-8", errors="replace")
    if launcher.returncode or launcher.stderr or "Hawk -" not in launcher_output:
        raise RuntimeError(f"Launcher verification failed: {launcher_output}")
    print("Launcher menu displayed and exited successfully", flush=True)
    commands = [
        ["-c", "import sys,pathlib,duckdb,httpx,yaml,loguru,pytest; "
         "root=pathlib.Path.cwd(); assert pathlib.Path(sys.prefix)==root/'runtime'; "
         "assert all(pathlib.Path(m.__file__).is_relative_to(root) "
         "for m in (duckdb,httpx,yaml,loguru,pytest)); "
         "print('Runtime and dependencies resolve inside extracted package')"],
        ["scripts/verify_continuation.py"],
        ["scripts/daily_recommend.py", "--date", "2026-10-09", "--limit", "60",
         "--out", "data/package-smoke-recommend.json"],
        ["-m", "pytest", "tests", "-q"],
    ]
    results = [{"command": ["cmd.exe", "/d", "/c", "Start-Hawk.cmd"],
                "returncode": launcher.returncode, "output": launcher_output}]
    for command in commands:
        result = subprocess.run([str(python), "-E", "-s", *command], cwd=root,
                                env=environment, capture_output=True, timeout=180)
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        print(output[-4000:], flush=True)
        results.append({"command": command, "returncode": result.returncode,
                        "output": output})
        if result.returncode:
            raise RuntimeError(f"Package verification failed: {command}")
    report = archive.with_suffix(".verification.json")
    report.write_text(json.dumps({"archive": str(archive), "extracted": str(root),
                                  "hashes_verified": len(manifest["files"]),
                                  "results": results}, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(f"Verified package: {archive}\nReport: {report}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
