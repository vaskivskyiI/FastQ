"""Derived values: quantities computed from instrument readings with an experiment-specific formula
(e.g. a two-terminal sample resistance from the lock-in X and the excitation through a series
resistor). Instruments publish only what they measure; how a value is derived belongs to the
experiment, so the definitions live here, are recorded in every run (md["derived"]) and are shown
with their formula wherever the value appears.

<data>/derived.json (edited from the dashboard; lab.yaml derived.values seeds it):
  {"values": [{"name": "RSample", "units": "Ohm",
               "expr": "x / ((amp / sqrt(2) - x) / series_r)",
               "inputs": {"x": "mfli1.x", "amp": "mfli1.amplitude"},
               "constants": {"series_r": 6.7e6},
               "doc": "two-terminal: X across the sample, excitation through series_r"}]}

inputs are "<device>.<attr>" (attr as in ophyd/schema). Three users:
  * ioc_host/iocs/derived_ioc.py  live values (dashboard, history), prefix lab derived.prefix
  * fastq.experiment              md["derived"] = definitions + the data key of every input
  * fastq.analysis                columns computed per event from the recorded raw data,
                                  inputs from other streams interpolated in time
"""
from __future__ import annotations

import ast
import json
import math
import re
from pathlib import Path

import numpy as np

FUNCS = {"sqrt": np.sqrt, "abs": np.abs, "log": np.log, "log10": np.log10, "exp": np.exp, "sin": np.sin,
         "cos": np.cos, "tan": np.tan, "arctan2": np.arctan2, "hypot": np.hypot, "where": np.where,
         "minimum": np.minimum, "maximum": np.maximum}
CONSTS = {"pi": math.pi, "e": math.e}
_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Call, ast.Name, ast.Load, ast.Constant, ast.Compare,
          ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod, ast.USub, ast.UAdd, ast.FloorDiv,
          ast.Lt, ast.Gt, ast.LtE, ast.GtE, ast.Eq, ast.NotEq)
NAME = re.compile(r"^[A-Z][A-Za-z0-9]{0,30}$")


def compile_expr(expr: str, names: set[str]):
    """Safe arithmetic only: numbers, the given names, FUNCS / CONSTS. Returns a code object."""
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _NODES):
            raise ValueError(f"{expr!r}: {type(node).__name__} not allowed")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in FUNCS):
            raise ValueError(f"{expr!r}: only {sorted(FUNCS)} can be called")
        if isinstance(node, ast.Name) and node.id not in names | set(FUNCS) | set(CONSTS):
            raise ValueError(f"{expr!r}: unknown name {node.id!r} (inputs/constants: {sorted(names)})")
    return compile(tree, "<derived>", "eval")


def validate(defs: list[dict]) -> list[dict]:
    out, seen = [], set()
    for d in defs:
        name = str(d.get("name", "")).strip()
        if not NAME.match(name):
            raise ValueError(f"name {name!r}: CamelCase letters/digits, starting upper case (it becomes a PV)")
        if name in seen:
            raise ValueError(f"{name} defined twice")
        seen.add(name)
        inputs = {str(k): str(v) for k, v in (d.get("inputs") or {}).items()}
        for k, v in inputs.items():
            if not re.match(r"^[a-z_][a-z0-9_]*$", k) or not re.match(r"^[a-z0-9_]+\.[a-z0-9_]+$", v):
                raise ValueError(f"{name}: input {k}: {v!r} must be name: device.attr")
        constants = {str(k): float(v) for k, v in (d.get("constants") or {}).items()}
        compile_expr(str(d.get("expr", "")), set(inputs) | set(constants))
        out.append({"name": name, "expr": str(d["expr"]), "inputs": inputs, "constants": constants,
                    "units": str(d.get("units", ""))[:8], "doc": str(d.get("doc", ""))})
    return out


def _file(lab: dict) -> Path:
    from fastq.session import data_root

    return data_root(lab) / "derived.json"


def load(lab: dict) -> list[dict]:
    try:
        return validate(json.loads(_file(lab).read_text(encoding="utf-8")).get("values", []))
    except FileNotFoundError:
        return validate((lab.get("derived") or {}).get("values") or [])


def save(lab: dict, defs: list[dict]) -> list[dict]:
    clean = validate(defs)
    f = _file(lab)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"values": clean}, indent=1), encoding="utf-8")
    tmp.replace(f)
    return clean


def evaluate(d: dict, inputs: dict):
    code = compile_expr(d["expr"], set(d["inputs"]) | set(d["constants"]))
    with np.errstate(all="ignore"):
        return eval(code, {"__builtins__": {}}, {**FUNCS, **CONSTS, **d["constants"], **inputs})  # noqa: S307


def constant(lab: dict, name: str, default: float | None = None) -> float | None:
    """A constant of the active definitions by name (first match), e.g. the series resistor."""
    for d in load(lab):
        if name in d["constants"]:
            return d["constants"][name]
    return default


def describe(d: dict) -> str:
    consts = ", ".join(f"{k} = {v:g}" for k, v in d["constants"].items())
    ins = ", ".join(f"{k} = {v}" for k, v in d["inputs"].items())
    return f"{d['name']} = {d['expr']}  ({ins}{'; ' + consts if consts else ''})"


def run_md(lab: dict, devices: dict) -> list[dict]:
    """Definitions with the data key of each input (as the run records it)."""
    out = []
    for d in load(lab):
        keys = {}
        for k, ref in d["inputs"].items():
            dev, attr = ref.split(".", 1)
            sig = getattr(devices.get(dev), attr, None)
            keys[k] = sig.name if sig is not None else f"{dev}_{attr}"
        out.append({**d, "keys": keys})
    return out


def add_columns(df, defs: list[dict], wanted: set[str] | None = None, other=None):
    """Add derived columns to a stream table (pandas). other(key) -> (time, values) of a data key
    found in another stream (interpolated onto this stream's time), or None."""
    for d in defs:
        if wanted is not None and d["name"] not in wanted:
            continue
        ins, ok = {}, True
        for k, key in (d.get("keys") or {}).items():
            if key in df.columns:
                ins[k] = df[key].to_numpy(dtype=float)
            elif other is not None and "time" in df.columns and (src := other(key)) is not None:
                ins[k] = np.interp(df["time"].to_numpy(dtype=float), *src)
            else:
                ok = False
                break
        if ok:
            df[d["name"]] = np.broadcast_to(evaluate(d, ins), (len(df),)).astype(float)
    return df
