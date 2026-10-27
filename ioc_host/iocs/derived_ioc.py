"""IOC: derived values (control host) - live computation of the experiment's formulas
(control/fastq/derived.py) from instrument PVs, for dashboards, history and monitoring.
Runs (in plans and analysis) compute the same formulas per event from the recorded raw data.

    python ioc_host/iocs/derived_ioc.py --lab control/lab.yaml --prefix FASTQ:DERIVED:

Inputs "<device>.<attr>" are resolved through the directory (FASTQ_DIRECTORY) and each device's
IOC:SCHEMA. When derived.json changes the IOC exits so that start_control_host restarts it with
the new PVs. A value is NaN while an input is missing.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "control"))

from ioc_host.common.devkit import Instrument, Param, run_instrument  # noqa: E402

log = logging.getLogger("derived_ioc")


def _lab_path() -> str:
    for i, a in enumerate(sys.argv):
        if a == "--lab" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith("--lab="):
            return a.split("=", 1)[1]
    return os.environ.get("FASTQ_LAB", str(ROOT / "control" / "lab.yaml"))


def _definitions() -> tuple[dict, list[dict]]:
    from fastq import derived
    from fastq.session import load_lab

    lab = load_lab(_lab_path())
    return lab, derived.load(lab)


def _make_class(defs: list[dict]) -> type:
    from fastq import derived as D

    attrs = {}
    for d in defs:
        attrs[d["name"]] = Param(float, units=d["units"], precision=6, kind="hinted", trend=True, overview=True,
                                 get=(lambda s, d=d: s.compute(d)), doc="derived: " + D.describe(d)
                                 + (f" - {d['doc']}" if d["doc"] else ""))
    return type("Derived", (DerivedBase,), attrs)


class DerivedBase(Instrument):
    TYPE = "derived"
    DESCRIPTION = "Derived values of the current experiment (formulas in derived.json, computed live)"
    DEFAULT_PREFIX = "FASTQ:DERIVED:"
    POLL = 0.5

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--lab", help="lab file (default env FASTQ_LAB)")

    def instance_schema(self) -> dict:
        schema = self.schema()
        by_name = {d["name"]: d for d in self.defs}
        for p in schema["params"]:
            if p["name"] in by_name:
                p["derived"] = {k: by_name[p["name"]][k] for k in ("expr", "inputs", "constants", "doc")}
        return schema

    def __init__(self, args) -> None:
        super().__init__(args)
        self.lab, self.defs = _definitions()
        self.values_in: dict[str, float] = {}
        self._subs: list = []

    def open(self) -> None:
        from caproto.threading.client import Context

        from fastq.discovery import device_name, read_directory

        prefixes = {device_name(e["prefix"]): e["prefix"] for e in read_directory()}
        ctx = Context()
        schemas: dict[str, dict] = {}
        for d in self.defs:
            for ref in d["inputs"].values():
                if ref in self.values_in:
                    continue
                dev, attr = ref.split(".", 1)
                if dev not in prefixes:
                    raise ConnectionError(f"{ref}: device {dev} not in the directory")
                if dev not in schemas:
                    pv, = ctx.get_pvs(prefixes[dev] + "IOC:SCHEMA")
                    schemas[dev] = json.loads(bytes(pv.read(timeout=5).data).rstrip(b"\0").decode())
                p = next((p for p in schemas[dev]["params"] if p["attr"] == attr), None)
                if p is None:
                    raise ValueError(f"{ref}: {dev} has no param {attr}")
                name = prefixes[dev] + p["name"] + ("_RBV" if p.get("rbv") else "")
                pv, = ctx.get_pvs(name)
                self.values_in[ref] = float("nan")

                def cb(sub, resp, ref=ref):
                    self.values_in[ref] = float(resp.data[0])
                sub = pv.subscribe()
                sub.add_callback(cb)
                self._subs.append((sub, cb))
        self.drv = ctx
        threading.Thread(target=self._watch, daemon=True).start()

    def close(self) -> None:
        pass

    def compute(self, d: dict) -> float:
        from fastq import derived as D

        return float(D.evaluate(d, {k: self.values_in.get(ref, float("nan")) for k, ref in d["inputs"].items()}))

    def _watch(self) -> None:
        from fastq import derived as D

        f = D._file(self.lab)
        seen = f.stat().st_mtime if f.exists() else None
        while True:
            time.sleep(2)
            now = f.stat().st_mtime if f.exists() else None
            if now != seen:
                log.warning("%s changed - exiting to be restarted with the new definitions", f)
                os._exit(0)


if __name__ == "__main__":
    run_instrument(_make_class(_definitions()[1]))
