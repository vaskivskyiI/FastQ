"""Live state of the current run for the dashboards: a RunEngine subscriber that keeps
<data>/live.json up to date (atomic writes, at most every 0.5 s):

  {"uid", "scan_id", "plan_name", "comment", "sample", "start_time", "num_points", "shape",
   "plots", "plot_hints", "events", "last_event_time", "position": {scalar values of the last primary event},
   "exit_status" (null while running), "stop_time"}

Cheaper and more immediate than asking the data server, and it works while Tiled lags.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

KEEP = ("scan_id", "plan_name", "comment", "sample", "num_points", "shape", "plots", "plot_hints", "run_kind",
        "setup", "motors", "detectors", "elog_logbook")


def live_file(lab: dict) -> Path:
    from fastq.session import data_root

    return data_root(lab) / "live.json"


def read_live(lab: dict) -> dict | None:
    try:
        return json.loads(live_file(lab).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_INSTANCES: list = []


def note_suspension(why: str | None) -> None:
    """A suspender tripped (why) or cleared (None) - shown in the control room."""
    for inst in _INSTANCES:
        inst.state["suspended"] = why
        inst._write(force=True)


class LiveStatus:
    def __init__(self, lab: dict, period_s: float = 0.5) -> None:
        self.path = live_file(lab)
        self.period_s = period_s
        self.state: dict = {}
        self.primary: set[str] = set()
        self._last_write = 0.0
        _INSTANCES.append(self)

    def _write(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < self.period_s:
            return
        self._last_write = now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, default=str), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass  # never disturb a run because of the dashboard

    def __call__(self, name: str, doc: dict) -> None:
        try:
            getattr(self, name, lambda d: None)(doc)
        except Exception:  # noqa: BLE001
            pass

    def start(self, doc):
        self.state = {"uid": doc["uid"], "start_time": doc["time"], "events": 0, "position": {},
                      "exit_status": None, **{k: doc.get(k) for k in KEEP}}
        self.primary = set()
        self._write(force=True)

    def descriptor(self, doc):
        if doc.get("name") == "primary":
            self.primary.add(doc["uid"])

    def event(self, doc):
        if doc["descriptor"] not in self.primary:
            return
        self.state["events"] = doc["seq_num"]
        self.state["last_event_time"] = doc["time"]
        self.state["position"] = {k: v for k, v in doc["data"].items()
                                  if isinstance(v, (int, float, str, bool)) and len(str(v)) < 40}
        self._write()

    def event_page(self, doc):
        from event_model import unpack_event_page

        for ev in unpack_event_page(doc):
            self.event(ev)

    def stop(self, doc):
        self.state.update(exit_status=doc.get("exit_status"), stop_time=doc.get("time"), reason=doc.get("reason"))
        self._write(force=True)
