"""Run numbers and code provenance.

Run numbers (scan_id) come from a counter file <data>/scan_id.txt - they never
reset, whichever process runs the plans (queue server, interactive session).
The counter starts after the highest scan_id found in the JSONL backups.

Code archive: when the plans are loaded, all plan and library code
(control/experiments/*.py, control/fastq/**/*.py) is zipped to
<data>/code/<version>.zip (version = content hash, stored once). Every run
records that version plus its plan's source file, so it can be re-run exactly
even after the plan was changed or removed:

    python -m fastq.provenance show <run uid>            # plan call + code version
    python -m fastq.provenance extract <version> <dir>   # the complete code of that version
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import time
import zipfile
from pathlib import Path

CONTROL = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ run numbers
class _FileLock:
    def __init__(self, path: Path, timeout: float = 10.0) -> None:
        self.path, self.timeout = path, timeout

    def __enter__(self):
        t0 = time.monotonic()
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                if time.time() - self.path.stat().st_mtime > 30:  # left behind by a crashed process
                    self.path.unlink(missing_ok=True)
                elif time.monotonic() - t0 > self.timeout:
                    raise TimeoutError(f"run-number lock {self.path} busy")
                time.sleep(0.05)

    def __exit__(self, *exc):
        os.close(self.fd)
        self.path.unlink(missing_ok=True)


def _highest_recorded_scan_id(backup_dir: Path) -> int:
    best = 0
    for f in backup_dir.glob("*.jsonl") if backup_dir.exists() else []:
        try:
            with open(f, encoding="utf-8") as fh:
                name, doc = json.loads(fh.readline())
            if name == "start":
                best = max(best, int(doc.get("scan_id") or 0))
        except Exception:  # noqa: BLE001 - unreadable file: ignore
            continue
    return best


def persistent_scan_id_source(lab: dict):
    """RunEngine(scan_id_source=...): next run number from <data>/scan_id.txt."""
    from fastq.session import backup_dir, data_root

    counter = data_root(lab) / "scan_id.txt"

    def source(md) -> int:
        counter.parent.mkdir(parents=True, exist_ok=True)
        with _FileLock(counter.with_suffix(".lock")):
            last = int(counter.read_text().strip()) if counter.exists() else _highest_recorded_scan_id(backup_dir(lab))
            new = last + 1
            tmp = counter.with_suffix(".tmp")
            tmp.write_text(str(new))
            tmp.replace(counter)
        return new

    return source


# ------------------------------------------------------------------ code archive
def _code_files() -> list[Path]:
    # every file of the experiment folders (plans read data files such as fdm_integrators.csv) + the library
    from fastq.plan_registry import experiment_dirs

    files = [f for d in experiment_dirs() for f in sorted(d.iterdir()) if f.is_file() and f.suffix != ".pyc"]
    files += sorted((CONTROL / "fastq").rglob("*.py"))
    return [f for f in files if "__pycache__" not in f.parts]


def archive_code(lab: dict) -> dict:
    """Zip the current plan + library code into <data>/code/<version>.zip (once per version)."""
    from fastq.session import data_root

    version, contents = _hash_files()
    target = data_root(lab) / "code" / f"{version}.zip"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for rel, data in contents.items():
                z.writestr(rel, data)
            z.writestr("VERSION.json", json.dumps({"version": version, "created": time.time(),
                                                   "files": sorted(contents)}, indent=1))
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(buf.getvalue())
        tmp.replace(target)
    return {"version": version, "archive": f"code/{version}.zip"}


def archived_file(lab: dict, version: str, rel_path: str) -> bytes:
    from fastq.session import data_root

    with zipfile.ZipFile(data_root(lab) / "code" / f"{version}.zip") as z:
        return z.read(rel_path)


def current_version() -> str:
    """Version (content hash) of the code on disk now - same hash as archive_code()."""
    return _hash_files()[0]


def _hash_files() -> tuple[str, dict]:
    h = hashlib.sha256()
    contents = {}
    for f in _code_files():  # private experiment files are stored as experiments/<name> too
        rel = f.relative_to(CONTROL).as_posix() if f.is_relative_to(CONTROL) else f"experiments/{f.name}"
        data = f.read_bytes()
        contents[rel] = data
        h.update(rel.encode() + b"\0" + data + b"\0")
    return h.hexdigest()[:12], contents


def restore_plan(lab: dict, version: str, plan_file: str, function: str) -> tuple[str, Path]:
    """Put an archived plan back as <data>/restored/restored_<function>_<version>.py, offered as plan
    "<function>_<version>" next to the current one. Outside the code tree, so restoring an old plan
    does not change the code version of later runs."""
    from fastq.session import data_root

    source = archived_file(lab, version, plan_file).decode("utf-8")
    name = f"{function}_{version}"
    target = data_root(lab) / "restored" / f"restored_{name}.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    # comments only: the archived file may start with its docstring + `from __future__ import ...`
    header = (f"# Plan {function!r} restored from code version {version} ({plan_file}, <data>/code/{version}.zip).\n"
              f"# Delete this file to remove the plan again.\n")
    footer = (f"\n\n# ---- restored plan (only this one is offered)\n"
              f"{function}.__name__ = {name!r}\n"
              f"{function}.__qualname__ = {name!r}\n"
              f"PLANS = [{function}]\n")
    target.write_text(header + source + footer, encoding="utf-8")
    return name, target


def main() -> None:
    import argparse

    from fastq.session import data_root, load_lab

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show")
    s.add_argument("uid")
    e = sub.add_parser("extract")
    e.add_argument("version")
    e.add_argument("dest")
    a = ap.parse_args()
    lab = load_lab()
    if a.cmd == "extract":
        with zipfile.ZipFile(data_root(lab) / "code" / f"{a.version}.zip") as z:
            z.extractall(a.dest)
        print(f"code version {a.version} -> {a.dest}  (run it: cd {a.dest} && python -i -m fastq.startup)")
    else:
        from fastq.session import backup_dir

        for f in backup_dir(lab).glob(f"*_{a.uid[:8]}.jsonl"):
            with open(f, encoding="utf-8") as fh:
                _, start = json.loads(fh.readline())
            print(json.dumps({k: start.get(k) for k in ("scan_id", "plan_name", "plan_request", "roles", "code")},
                             indent=1))


if __name__ == "__main__":
    main()
