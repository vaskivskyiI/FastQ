"""Current experiment state: sample name and ELOG notebook, changeable at any time
(dashboard Overview / ELOG page) without restarting anything.

Stored in <data>/experiment.json; lab.yaml gives the defaults (metadata.sample,
elog.default_experiment). Every run records the values valid when it starts
(RunEngine preprocessor, see session.make_run_engine) - a plan's own
md={"sample": ...} still takes precedence.
"""
from __future__ import annotations

import json
import time
from collections import ChainMap
from pathlib import Path

FIELDS = ("sample", "notebook")


def _file(lab: dict) -> Path:
    from fastq.session import data_root

    root = data_root(lab)
    root.mkdir(parents=True, exist_ok=True)
    return root / "experiment.json"


def get_state(lab: dict) -> dict:
    """{"sample", "notebook", "changed"}: the saved values, else the lab.yaml defaults."""
    state = {"sample": (lab.get("metadata") or {}).get("sample"),
             "notebook": (lab.get("elog") or {}).get("default_experiment")}
    f = _file(lab)
    if f.exists():
        state.update({k: v for k, v in json.loads(f.read_text(encoding="utf-8")).items() if v not in (None, "")})
    return state


def set_state(lab: dict, **changes) -> dict:
    unknown = set(changes) - set(FIELDS)
    if unknown:
        raise ValueError(f"unknown experiment fields {sorted(unknown)}")
    state = get_state(lab)
    state.update({k: (str(v).strip() or None) if v is not None else None for k, v in changes.items()})
    state["changed"] = time.time()
    tmp = _file(lab).with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(_file(lab))
    return state


def run_md(lab: dict) -> dict:
    s = get_state(lab)
    md = {}
    if s.get("sample"):
        md["sample"] = s["sample"]
    if s.get("notebook"):
        md["elog_logbook"] = s["notebook"]
    try:  # how this experiment derives quantities (fastq.derived): analysis computes them from the raw data
        from fastq import derived
        from fastq.session import _DEVICES

        defs = derived.run_md(lab, _DEVICES)
        if defs:
            md["derived"] = defs
    except Exception:  # noqa: BLE001 - a bad definition must not stop a run
        pass
    return md


def experiment_preprocessor(lab: dict):
    """RunEngine preprocessor: every run gets the CURRENT sample / notebook
    (read when the plan starts); keys the plan sets itself win."""
    from bluesky.preprocessors import msg_mutator

    def preprocessor(plan):
        md = run_md(lab)

        def mutate(msg):
            if msg.command == "open_run":
                msg = msg._replace(kwargs=ChainMap(dict(msg.kwargs), md))
            return msg

        return msg_mutator(plan, mutate)

    return preprocessor
