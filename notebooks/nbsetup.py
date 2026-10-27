"""Shared setup of the FastQ analysis notebooks (imported by every notebook in this folder).

    from nbsetup import *

finds control/ (this repository, or FASTQ_CONTROL), uses the lab file FASTQ_LAB (default
control/lab.yaml), connects to the lab's data server (Tiled) and offers a few helpers:

    pick_run(ref, plan=..., kind=..., columns=[...])   one run (RunView), or a clear NoRun message
    find_runs(plan=..., kind=..., sample=..., text=..., columns=[...], limit=...)   newest first
    recent(limit)                                      summary table of the newest runs
    draw(ax, out)                                      draw a fastq.analysis.plot_data result
    dashboard(path, **params)                          GET from the dashboard API (FASTQ_URL)
"""
from __future__ import annotations

import fnmatch
import os
import sys
from datetime import datetime
from pathlib import Path


def _control_dir() -> Path:
    if os.environ.get("FASTQ_CONTROL"):
        return Path(os.environ["FASTQ_CONTROL"]).resolve()
    for start in (Path(__file__).resolve().parent, Path.cwd().resolve()):
        for p in (start, *start.parents):
            if (p / "control" / "fastq" / "analysis.py").exists():
                return p / "control"
    raise RuntimeError("FastQ control/ folder not found - start Jupyter inside the repository or set FASTQ_CONTROL")


CONTROL = _control_dir()
if str(CONTROL) not in sys.path:
    sys.path.insert(0, str(CONTROL))
os.environ.setdefault("FASTQ_LAB", str(CONTROL / "lab.yaml"))

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from fastq import analysis as A  # noqa: E402
from fastq import derived as D  # noqa: E402
from fastq.analysis import RunView, data_keys, default_plots, export_hdf5, plot_data  # noqa: E402
from fastq.session import load_lab  # noqa: E402

LOCAL_TZ = datetime.now().astimezone().tzinfo
EXPORTS = Path(__file__).resolve().parent / "exports"   # git-ignored
DASHBOARD_URL = os.environ.get("FASTQ_URL", "http://127.0.0.1:8088").rstrip("/")


class NoRun(LookupError):
    """No run matches - shown as a plain message in Jupyter (no traceback)."""

    def _render_traceback_(self):
        return [f"NoRun: {self}"]


lab = load_lab()
TILED_URI = (lab.get("data") or {}).get("tiled_uri") or "http://127.0.0.1:8000"
_client = None


def client():
    global _client
    if _client is None:
        try:
            _client = A.tiled_client(lab)
        except Exception as exc:  # noqa: BLE001
            raise NoRun(f"cannot reach the data server {TILED_URI} (lab file {lab['_path']}): "
                        f"{type(exc).__name__}: {exc}") from None
    return _client


def _newest(node, limit: int):
    """(uid, run node) pairs, newest first (a Tiled page holds at most 300)."""
    n = min(len(node), limit, 300)
    if not n:
        return []
    return list(node.items()[-1:-n - 1:-1])


def _has_columns(node, patterns, stream="primary") -> bool:
    if not patterns:
        return True
    if stream not in node:
        return False
    cols = list(data_keys(node, stream))
    return all(any(fnmatch.fnmatchcase(c, p) for c in cols) for p in patterns)


def find_runs(plan: str | None = None, kind: str | None = None, sample: str | None = None,
              text: str | None = None, status: str | None = "success", columns: list | None = None,
              stream: str = "primary", limit: int = 20, scan: int = 300) -> list[RunView]:
    """Runs newest first. plan / kind / sample: exact plan_name / run_kind / sample (server-side
    query); text: words that must all appear in plan, kind, comment or sample; status: exit_status
    (None = any, also running); columns: glob patterns that must all match a column of `stream`."""
    from tiled.queries import Key

    node = client()
    for key, value in (("start.plan_name", plan), ("start.run_kind", kind), ("start.sample", sample),
                       ("stop.exit_status", status)):
        if value is not None:
            node = node.search(Key(key) == value)
    words = [w.lower() for w in (text or "").split()]
    out = []
    for uid, run in _newest(node, scan):
        md = (run.metadata or {}).get("start") or {}
        hay = " ".join(str(md.get(k) or "") for k in ("plan_name", "run_kind", "comment", "sample", "scan_id")).lower()
        if not all(w in hay for w in words):
            continue
        if columns and stream not in run:
            continue
        if not _has_columns(run, columns, stream):
            continue
        out.append(RunView(uid, run))
        if len(out) >= limit:
            break
    return out


def _describe(**crit) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in crit.items() if v not in (None, [], ""))


def pick_run(ref=None, required: bool = True, **crit) -> RunView | None:
    """ref: a scan number (905), -1 / -2 (latest / second latest run of any kind), "latest", or a
    uid (prefix); None = the newest run matching crit (see find_runs). required=False returns None
    instead of raising NoRun."""
    try:
        if ref is not None:
            run = A.load_run(ref, client=client())
            if crit.get("columns") and not _has_columns(run.node, crit["columns"], crit.get("stream", "primary")):
                raise NoRun(f"run {ref!r} ({run}) has no columns {crit['columns']}")
            return run
        runs = find_runs(limit=1, **crit)
        if runs:
            return runs[0]
        raise NoRun(f"no run with {_describe(**crit)} on {TILED_URI} (lab {Path(lab['_path']).name}) - "
                    "record one first or set the run number at the top of this section")
    except KeyError as exc:
        err = NoRun(f"{exc.args[0] if exc.args else exc} on {TILED_URI}")
    except NoRun as exc:
        err = exc
    if required:
        raise err
    print(f"(skipped) {err}")
    return None


def to_local(t):
    """Epoch seconds -> local naive datetimes (for time axes)."""
    return pd.to_datetime(np.asarray(t, dtype=float), unit="s", utc=True).tz_convert(LOCAL_TZ).tz_localize(None)


def run_row(run: RunView) -> dict:
    s = A.summary(run.uid, run.node)
    t0, t1 = s.get("time"), s.get("stop_time")
    return {"scan_id": s["scan_id"], "plan": s["plan_name"], "run_kind": s["run_kind"], "sample": s["sample"],
            "comment": s["comment"], "status": s["exit_status"] or "running",
            "start": to_local([t0])[0].floor("s") if t0 else None,
            "duration_s": round(t1 - t0, 1) if t0 and t1 else None,
            "events": s["num_events"], "uid": run.uid[:8]}


def recent(limit: int = 30, **crit) -> pd.DataFrame:
    """Summary table of the newest runs (any status unless status=... is given)."""
    crit.setdefault("status", None)
    return pd.DataFrame([run_row(r) for r in find_runs(limit=limit, **crit)])


def label(run: RunView) -> str:
    c = run.md.get("comment") or run.md.get("run_kind") or run.md.get("plan_name")
    return f"#{run.md.get('scan_id')} {c}"


def draw(ax, out: dict, **kw):
    """Draw the result of fastq.analysis.plot_data (series with optional "err") on a matplotlib axis."""
    if out.get("kind") == "heatmap":
        if out.get("z"):
            m = ax.pcolormesh(out["x"], out["y"], np.array(out["z"], dtype=float), shading="nearest")
            plt.colorbar(m, ax=ax, label=out.get("z_label"))
    else:
        for s in out.get("series", []):
            x, y = np.array(s["x"], dtype=float), np.array(s["y"], dtype=float)
            if out.get("kind") == "scatter":
                ax.plot(x, y, "o", ms=3, label=s["name"], **kw)
            elif s.get("err") is not None:
                ax.errorbar(x, y, yerr=np.array(s["err"], dtype=float), marker="o", ms=3, capsize=2,
                            label=s["name"], **kw)
            else:
                ax.plot(x, y, marker=".", label=s["name"], **kw)
        if out.get("series"):
            ax.legend(fontsize=8)
    ax.set_xlabel(out.get("x_label", ""))
    ax.set_ylabel(out.get("y_label", ""))
    if out.get("title"):
        ax.set_title(out["title"])
    return ax


_dash: dict = {}


def dashboard(path: str, timeout: float = 10, **params):
    """GET <FASTQ_URL><path> (dashboard API, JSON). With dashboard accounts: FASTQ_USER / FASTQ_PASSWORD
    (a viewer account is enough) - logged in once per kernel."""
    import httpx

    if "client" not in _dash:
        _dash["client"] = httpx.Client(base_url=DASHBOARD_URL, timeout=timeout)
        if os.environ.get("FASTQ_USER"):
            _dash["client"].post("/api/login", json={"user": os.environ["FASTQ_USER"],
                                                     "password": os.environ.get("FASTQ_PASSWORD", "")}).raise_for_status()
    r = _dash["client"].get(path, params=params)
    r.raise_for_status()
    return r.json()


print(f"FastQ lab {lab['_path']}  data server {TILED_URI}  dashboard {DASHBOARD_URL}")

__all__ = ["A", "D", "CONTROL", "EXPORTS", "DASHBOARD_URL", "TILED_URI", "LOCAL_TZ", "NoRun", "RunView", "lab",
           "client", "find_runs", "pick_run", "recent", "run_row", "label", "draw", "dashboard", "to_local",
           "data_keys", "default_plots", "export_hdf5", "plot_data", "np", "pd", "plt", "Path"]
