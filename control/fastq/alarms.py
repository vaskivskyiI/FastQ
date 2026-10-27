"""Alarm rules on device values (evaluated by the web server on every update of the value).

<data>/alarms.json (edited on the dashboard System page; lab.yaml alarms: seeds it):
  {"rules": [{"name": "Cryostat drift", "ref": "itc1.temp", "above": 12.0, "below": null,
              "rate_per_min": 0.5, "for_s": 60, "actions": ["notify", "elog"], "enabled": true,
              "logbook": null, "note": "check the heater / gas"}]}

ref = "<device>.<attr>" (like derived inputs) or "derived.<Name>". A rule is TRUE while the value is
above `above`, below `below`, or changes faster than |rate_per_min| (slope of the last for_s). It
becomes ACTIVE after being true for for_s (no alarms from single spikes) and CLEARS after being false
for for_s / 2. Activation and clearing are posted: "notify" = dashboard toast + desktop notification
(/api/alarms), "elog" = an entry in the rule's logbook or the current experiment notebook.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from pathlib import Path

ACTIONS = ("notify", "elog")


def _file(lab: dict) -> Path:
    from fastq.session import data_root

    return data_root(lab) / "alarms.json"


def validate(rules: list[dict]) -> list[dict]:
    out, names = [], set()
    for r in rules:
        name = str(r.get("name", "")).strip()
        if not name or name in names:
            raise ValueError(f"alarm name {name!r} missing or used twice")
        names.add(name)
        ref = str(r.get("ref", "")).strip()
        if "." not in ref:
            raise ValueError(f"{name}: ref must be device.attr (e.g. itc1.temp)")
        num = lambda k: None if r.get(k) in (None, "") else float(r[k])  # noqa: E731
        rule = {"name": name, "ref": ref, "above": num("above"), "below": num("below"),
                "rate_per_min": num("rate_per_min"), "for_s": max(0.0, num("for_s") or 0.0),
                "actions": [a for a in (r.get("actions") or ["notify"]) if a in ACTIONS],
                "enabled": bool(r.get("enabled", True)), "logbook": r.get("logbook") or None,
                "note": str(r.get("note") or "")}
        if rule["above"] is None and rule["below"] is None and rule["rate_per_min"] is None:
            raise ValueError(f"{name}: give above, below or rate_per_min")
        out.append(rule)
    return out


def load(lab: dict) -> list[dict]:
    try:
        return validate(json.loads(_file(lab).read_text(encoding="utf-8")).get("rules", []))
    except FileNotFoundError:
        return validate(lab.get("alarms") or [])


def save(lab: dict, rules: list[dict]) -> list[dict]:
    clean = validate(rules)
    f = _file(lab)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"rules": clean}, indent=1), encoding="utf-8")
    tmp.replace(f)
    return clean


class Engine:
    """State of every rule; update() with each new value; post(event) is called on activate / clear."""

    def __init__(self, rules: list[dict], post) -> None:
        self.post = post
        self._lock = threading.Lock()
        self.set_rules(rules)

    def set_rules(self, rules: list[dict]) -> None:
        with self._lock:
            old = getattr(self, "state", {})
            self.rules = {r["name"]: r for r in rules if r["enabled"]}
            self.state = {n: old.get(n) or {"active": False, "since": None, "false_since": None, "value": None,
                                             "t": None, "why": "", "hist": deque()} for n in self.rules}

    def refs(self) -> set[str]:
        return {r["ref"] for r in self.rules.values()}

    def _why(self, r: dict, st: dict, v: float) -> str:
        if r["above"] is not None and v > r["above"]:
            return f"{v:.6g} > {r['above']:g}"
        if r["below"] is not None and v < r["below"]:
            return f"{v:.6g} < {r['below']:g}"
        if r["rate_per_min"] is not None and len(st["hist"]) >= 2:
            (t0, v0), (t1, v1) = st["hist"][0], st["hist"][-1]
            if t1 - t0 >= min(30.0, max(r["for_s"], 5.0)):
                rate = (v1 - v0) / (t1 - t0) * 60
                if abs(rate) > abs(r["rate_per_min"]):
                    return f"changes {rate:+.3g}/min (limit {abs(r['rate_per_min']):g}/min)"
        return ""

    def update(self, ref: str, v, t: float | None = None) -> None:
        if isinstance(v, bool):
            v = float(v)
        if not isinstance(v, (int, float)) or math.isnan(v):
            return
        t = time.time() if t is None else float(t)
        events = []
        with self._lock:
            for name, r in self.rules.items():
                if r["ref"] != ref:
                    continue
                st = self.state[name]
                st["value"], st["t"] = float(v), t
                st["hist"].append((t, float(v)))
                window = max(r["for_s"], 60.0)
                while st["hist"] and st["hist"][0][0] < t - window:
                    st["hist"].popleft()
                why = self._why(r, st, float(v))
                if why:
                    st["false_since"] = None
                    st["since"] = st["since"] or t
                    if not st["active"] and t - st["since"] >= r["for_s"]:
                        st["active"], st["why"] = True, why
                        events.append({"rule": r, "kind": "active", "why": why, "value": float(v), "t": t})
                else:
                    st["since"] = None
                    if st["active"]:
                        st["false_since"] = st["false_since"] or t
                        if t - st["false_since"] >= r["for_s"] / 2:
                            st["active"], st["why"] = False, ""
                            events.append({"rule": r, "kind": "cleared", "why": "back to normal", "value": float(v),
                                           "t": t})
        for e in events:
            try:
                self.post(e)
            except Exception:  # noqa: BLE001 - an action failing must not stop the evaluation
                pass

    def status(self) -> list[dict]:
        with self._lock:
            return [{**{k: v for k, v in r.items()}, "active": self.state[n]["active"], "why": self.state[n]["why"],
                     "value": self.state[n]["value"], "t": self.state[n]["t"], "pending_since": self.state[n]["since"]}
                    for n, r in self.rules.items()]
