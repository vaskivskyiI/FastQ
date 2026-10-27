"""Automatic backup of the data folder (<data> = parent of data.backup_dir) to another disk / NAS.

lab.yaml:  backup: {target: '\\\\nas\\fastq\\4dmag', every_h: 6, keep: 14}

    python -m fastq.backup --now          one backup (start_control_host runs --loop as a helper)

<target>/mirror/   append-only folders, copied when new or changed (size / mtime): raw JSONL, Tiled
                   array files, code archive, ELOG outbox, restored plans
<target>/<stamp>/  every pass: the SQLite databases (Tiled catalog + tables, value history) through
                   SQLite's backup API - consistent while the servers write - and the state files
                   (*.json, *.yaml, *.jsonl); the newest `keep` passes are kept
Status (time, files, bytes, errors) in <data>/backup.json - shown on the dashboard System page.
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import sqlite3
import time
from pathlib import Path

MIRROR = ("jsonl", "tiled/files", "code", "elog", "restored")
DATABASES = ("tiled/catalog.db", "tiled/tables.sqlite", "history/history.db")


def _status_file(lab: dict) -> Path:
    from fastq.session import data_root

    return data_root(lab) / "backup.json"


def config(lab: dict) -> dict:
    b = dict(lab.get("backup") or {})
    return {"target": b.get("target"), "every_h": float(b.get("every_h", 6)), "keep": int(b.get("keep", 14))}


def _mirror(src: Path, dst: Path, stats: dict) -> None:
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        out = dst / f.relative_to(src)
        st = f.stat()
        if out.exists() and out.stat().st_size == st.st_size and out.stat().st_mtime >= st.st_mtime - 1:
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(f, out)
            stats["files"] += 1
            stats["bytes"] += st.st_size
        except OSError as exc:  # a file being written (an open HDF5): next pass
            stats["errors"].append(f"{f.name}: {exc}")


def _sqlite_copy(src: Path, dst: Path) -> None:
    s, d = sqlite3.connect(f"file:{src}?mode=ro", uri=True), sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        s.close()
        d.close()


def run_once(lab: dict) -> dict:
    from fastq.session import data_root, tiled_dir

    cfg = config(lab)
    if not cfg["target"]:
        raise ValueError("lab.yaml backup.target is not set")
    from fastq.session import CONTROL_DIR

    root, target = data_root(lab), Path(cfg["target"])
    target = target if target.is_absolute() or str(target).startswith("\\\\") else CONTROL_DIR.parent / target
    stamp = time.strftime("%Y%m%d-%H%M%S")
    snap = target / stamp
    snap.mkdir(parents=True, exist_ok=False)
    stats = {"time": time.time(), "target": str(target), "snapshot": stamp, "files": 0, "bytes": 0, "errors": [],
             "host": socket.gethostname()}
    tiled = tiled_dir(lab)
    for rel in MIRROR:
        src = tiled / rel.split("/", 1)[1] if rel.startswith("tiled/") else root / rel
        if src.exists():
            _mirror(src, target / "mirror" / rel, stats)
    for rel in DATABASES:
        src = tiled / rel.split("/", 1)[1] if rel.startswith("tiled/") else root / rel
        if src.exists():
            out = snap / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            try:
                _sqlite_copy(src, out)
                stats["files"] += 1
                stats["bytes"] += out.stat().st_size
            except sqlite3.Error as exc:
                stats["errors"].append(f"{rel}: {exc}")
    for f in list(root.glob("*.json")) + list(root.glob("*.jsonl")) + list(root.glob("*.yaml")) + \
            list((root / "history").glob("*.json")) + [Path(lab["_path"])] if lab.get("_path") else []:
        if f.is_file() and f.name != "backup.json":
            shutil.copy2(f, snap / f.name)
            stats["files"] += 1
    snaps = sorted(p for p in target.iterdir() if p.is_dir() and p.name[:8].isdigit())
    for old in snaps[:-cfg["keep"]]:
        shutil.rmtree(old, ignore_errors=True)
    stats["duration_s"] = round(time.time() - stats["time"], 1)
    stats["errors"] = stats["errors"][:50]
    _status_file(lab).write_text(json.dumps(stats, indent=1), encoding="utf-8")
    return stats


def status(lab: dict) -> dict | None:
    try:
        return json.loads(_status_file(lab).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main() -> None:
    from fastq.session import load_lab

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lab")
    ap.add_argument("--now", action="store_true", help="one backup now")
    ap.add_argument("--loop", action="store_true", help="a backup every backup.every_h (control-host helper)")
    a = ap.parse_args()
    lab = load_lab(a.lab)
    if a.loop:
        while True:
            last = (status(lab) or {}).get("time", 0)
            if time.time() - last >= config(lab)["every_h"] * 3600:
                try:
                    s = run_once(lab)
                    print(f"backup {s['snapshot']}: {s['files']} files, {s['bytes'] / 1e6:.1f} MB, "
                          f"{len(s['errors'])} errors", flush=True)
                except Exception as exc:  # noqa: BLE001 - the target may be offline: try again later
                    print(f"backup failed: {exc}", flush=True)
                    _status_file(lab).write_text(json.dumps({"time": time.time() - config(lab)["every_h"] * 3600
                                                             + 900, "error": str(exc)}), encoding="utf-8")
            time.sleep(300)
    else:
        print(json.dumps(run_once(lab), indent=1))


if __name__ == "__main__":
    main()
