"""Export runs from the data server - CSV per stream (e.g. for Origin) or one HDF5 file.

    python clients/export_run.py --list                 # recent runs
    python clients/export_run.py 905                    # run number 905: every stream -> CSV
    python clients/export_run.py -1 --hdf5              # the latest run -> one HDF5 (frames referenced)
    python clients/export_run.py 3097de9d --hdf5 --frames   # ... with the camera frames inside

Lab file: --lab or env FASTQ_LAB (gives the data server and its API key); runs by scan_id,
-1 / -n (latest), or uid prefix. Same code as the dashboard (control/fastq/analysis.py).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "control"))

from fastq import analysis as A  # noqa: E402
from fastq.session import load_lab  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run", nargs="?", help="scan_id, -1 (latest) or uid prefix")
    ap.add_argument("--lab", help="lab file (default env FASTQ_LAB or control/lab.yaml)")
    ap.add_argument("--stream", action="append", help="only these streams (CSV)")
    ap.add_argument("--hdf5", action="store_true", help="one HDF5 file instead of CSVs")
    ap.add_argument("--frames", action="store_true", help="HDF5: include the camera frames")
    ap.add_argument("--out", default=".")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    client = A.tiled_client(load_lab(a.lab))
    if a.list or not a.run:
        for uid, node in list(client.items()[-1:-21:-1]):
            r = A.summary(uid, node)
            print(f"{r['scan_id']!s:>6} {uid[:8]} {r['plan_name']:<22} {r['exit_status'] or 'running':<8} {r['comment'][:50]}")
        return
    uid, node = A.find_run(client, a.run)
    run = A.RunView(uid, node)
    stem = f"run{run.md.get('scan_id', '')}_{run.md.get('plan_name', 'run')}_{uid[:8]}"
    if a.hdf5:
        path = A.export_hdf5(run, Path(a.out) / f"{stem}.h5", frames=a.frames)
        print(path)
        return
    for stream in a.stream or run.streams:
        df = run.table(stream)
        path = Path(a.out) / f"{stem}_{stream}.csv"
        df.to_csv(path, index=False)
        print(f"{path}  ({len(df)} rows)")


if __name__ == "__main__":
    main()
