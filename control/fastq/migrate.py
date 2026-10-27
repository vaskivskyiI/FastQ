"""Old names -> new names (2026-10-27 renames: one naming per instrument class and per plan
parameter, recipes instead of wrapper plans). One table, two uses:

    python -m fastq.migrate                 # dry run: what would change in <data>
    python -m fastq.migrate --apply         # rewrite presets.json, plot_layouts.json, views.json,
                                            # derived.json and the PV names in history.db (backups *.bak)
                                            # - run it BEFORE the IOCs restart with the new names

and the dashboard's "fill from run" translates the plan_request of older runs on the fly
(translate_request), so a run from before the renames can be repeated with today's plans.
Device types come from the directory (device name -> type); --types itc1=itc503,... overrides.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

# ---------------------------------------------------------------- PV names per instrument type
_N = range(3, -1, -1)  # renumber 0-based -> 1-based from the top, so names never collide
PV_RENAMES: dict[str, list[tuple[str, str]]] = {
    "mfli": [("OscFreq", "Frequency"), ("TimeConst", "TimeConstant"), ("SigOutAmpEnable", "AmplitudeEnable"),
             ("SigOutAmp", "Amplitude"), ("PhaseShift", "Phase"), ("RefMode", "RefSource"),
             ("AuxIn1", "AuxIn2"), ("AuxIn0", "AuxIn1")]
            + [(f"AuxOut{k}{n}", f"AuxOut{k}{n + 1}") for n in _N
               for k in ("", "Select", "Value", "Scale", "Demod", "LimitLow", "LimitHigh")],
    "ametek_lockin": [("Slope", "FilterSlope"), ("RefPhase", "Phase"), ("OscFrequency", "Frequency"),
                      ("OscAmplitude", "Amplitude"), ("Reference", "RefSource")]
                     + [(f"DAC{n}", f"AuxOut{n}") for n in (1, 2, 3, 4)] + [(f"ADC{n}", f"AuxIn{n}") for n in (1, 2, 3, 4)],
    "itc503": [("SweepStatus", "SweepStep")],
    "pharos": [("OutputEnabled", "ShutterOpen"), ("PPEnabled", "PPEnable"), ("PulseCount", "BurstCount"),
               ("PPTriggerSource", "TriggerSource"), ("Status", "GeneralStatus")],
    "xeryon": [(r"(\w+)Speed", r"\1Velocity")],
    "arduino_stepper": [(r"(\w+)Speed", r"\1Velocity"), (r"(\w+)Accel", r"\1Acceleration")],
}


def snake(name: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    return re.sub(r"(?<=[A-Z])([A-Z][a-z])", r"_\1", s).lower()


def rename_pv(dev_type: str, name: str) -> str:
    """A PV name (suffix after the prefix, with or without _RBV) of a device of this type."""
    base, rbv = (name[:-4], "_RBV") if name.endswith("_RBV") else (name, "")
    for old, new in PV_RENAMES.get(dev_type, []):
        if "\\" in new or "(" in old:
            if re.fullmatch(old, base):
                return re.sub(f"^{old}$", new, base) + rbv
        elif base == old:
            return new + rbv
    return name


def rename_column(types: dict, column: str) -> str:
    """A data key <device>_<attr> (attr = snake of the PV name)."""
    for dev, t in types.items():
        if column.startswith(dev + "_"):
            attr = column[len(dev) + 1:]
            for old, new in PV_RENAMES.get(t, []):
                if "(" in old:
                    continue  # per-axis motor params are not event columns
                if attr == snake(old):
                    return f"{dev}_{snake(new)}"
    return column


def rename_ref(types: dict, ref: str) -> str:
    """A "device.attr" reference (derived inputs, nd_scan "dev.attr" axes)."""
    dev, _, attr = ref.partition(".")
    new = rename_column(types, f"{dev}_{attr}")
    return f"{dev}.{new[len(dev) + 1:]}" if new != f"{dev}_{attr}" else ref


# ---------------------------------------------------------------- plans and parameters
PARAM_RENAMES: dict[str, list[tuple[str, str]]] = {   # applied in order (a name may be renamed twice)
    "*": [("itc", "temperature"), ("excitation_current_a", "current_a")],
    "rt_sweep": [],
    "iv_curves": [("currents", "currents_a"), ("temperatures", "temperatures_k"), ("settle_s", "first_settle_s"),
                  ("settle_temperature_s", "settle_s")],
    "aux_waveform": [("index", "aux_index")],
    "laser_sequence_scan": [("temperatures", "temperatures_k"), ("settle_temperature_s", "settle_s")],
    "tm_rt_steps": [("temperatures", "temperatures_k"), ("table", "table_k"), ("wait_s", "settle_s"),
                    ("pulse_width_s", "width_s"), ("lockin_too", "with_lockin")],
    "tm_pulse_sweep": [("currents", "currents_a"), ("temperatures", "temperatures_k"), ("wait_s", "settle_s")],
    "fdm_static": [("label", "run_kind")],
    "fdm_background": [("label", "run_kind")],
    "fdm_delay_scan": [("delay_segments", "delay_segments_ps"), ("delays", "delays_ps")],
    "fdm_waveplate_scan": [("angles", "angles_deg"), ("start", "start_deg"), ("stop", "stop_deg"), ("step", "step_deg")],
    "fdm_rotation_scan": [("angles", "positions_steps"), ("start", "start_steps"), ("stop", "stop_steps"),
                          ("step", "step_steps")],
    "fdm_field_loop": [("currents", "currents_a"), ("delays", "delays_ps")],
    "fdm_fluence_scan": [("nd_positions", "nd_positions_steps"), ("start", "start_steps"), ("stop", "stop_steps"),
                         ("step", "step_steps")],
}


def _log(kw: dict, devices_key: str) -> dict:
    period = float(kw.pop("period_s", 1.0))
    n = max(1, round(float(kw.pop("duration_s", 300.0)) / period))
    devs = kw.pop(devices_key, None) or ["@temperature"]
    return {"axes": [{"repeat": n}], "readables": devs if isinstance(devs, list) else [devs], "period_s": period}


# removed plans -> (new plan, kwargs transform applied after the parameter renames of the old plan)
PLAN_RENAMES = {
    "tm_dc_iv": ("iv_curves", lambda kw: {({"temperatures": "temperatures_k", "settle_s": "first_settle_s"}).get(k, k): v
                                          for k, v in kw.items()}),
    "temperature_log": ("nd_scan", lambda kw: _log(kw, "controllers")),
}


def _private_rules() -> None:
    """migrate_rules.py of a private experiments folder (fastq.plan_registry.experiment_dirs) adds its
    PARAM_RENAMES / PLAN_RENAMES - renames of plans that are not in the public repository."""
    import importlib.util

    from fastq.plan_registry import experiment_dirs

    for d in experiment_dirs()[1:]:
        f = d / "migrate_rules.py"
        if f.exists():
            spec = importlib.util.spec_from_file_location(f"migrate_rules_{abs(hash(str(d)))}", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for k, v in getattr(mod, "PARAM_RENAMES", {}).items():
                PARAM_RENAMES.setdefault(k, []).extend(v)
            PLAN_RENAMES.update(getattr(mod, "PLAN_RENAMES", {}))


_private_rules()


def translate_request(name: str, kwargs: dict) -> tuple[str, dict, list[str]]:
    """(plan, kwargs) of an old run -> today's names; notes say what changed."""
    kw, notes = dict(kwargs or {}), []
    for old, new in PARAM_RENAMES["*"] + PARAM_RENAMES.get(name, []):
        if old in kw:
            kw[new] = kw.pop(old)
            notes.append(f"{old} -> {new}")
    if name == "aux_waveform" and "aux_index" in kw and "index -> aux_index" in notes:
        kw["aux_index"] = int(kw["aux_index"]) + 1   # the old index counted from 0
    if name in PLAN_RENAMES:
        new_name, fn = PLAN_RENAMES[name]
        kw = fn(kw)
        notes.append(f"plan {name} -> {new_name}")
        name = new_name
    return name, kw, notes


# ---------------------------------------------------------------- the files of a lab
def device_types(lab: dict) -> dict:
    try:
        import os

        if lab.get("directory"):
            os.environ.setdefault("FASTQ_DIRECTORY", lab["directory"])
        from fastq.discovery import device_name, read_directory

        return {device_name(e["prefix"]): e["type"] for e in read_directory()}
    except Exception as exc:  # noqa: BLE001
        print(f"directory not readable ({exc}) - pass --types name=type,...", file=sys.stderr)
        return {}


def _write(path: Path, data, apply: bool) -> None:
    if apply:
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def migrate(lab: dict, types: dict, apply: bool) -> list[str]:
    from fastq.session import data_root

    root, log = data_root(lab), []
    f = root / "presets.json"
    if f.exists():
        data, out = json.loads(f.read_text(encoding="utf-8")), {}
        for plan, ps in data.items():
            for pname, p in ps.items():
                new, kw, notes = translate_request(plan, p.get("kwargs") or {})
                out.setdefault(new, {})[pname] = {**p, "kwargs": kw}
                if notes:
                    log.append(f"preset {plan}/{pname}: {', '.join(notes)}")
        _write(f, out, apply and data != out)
    f = root / "plot_layouts.json"
    if f.exists():
        data = json.loads(f.read_text(encoding="utf-8"))
        out = {}
        for plan, specs in data.items():
            new_plan = PLAN_RENAMES.get(plan, (plan,))[0]
            new_specs = json.loads(json.dumps(specs))
            for spec in new_specs:
                for k in ("x", "group", "z"):
                    if isinstance(spec.get(k), str):
                        spec[k] = rename_column(types, spec[k])
                if isinstance(spec.get("y"), list):
                    spec["y"] = [rename_column(types, y) for y in spec["y"]]
                if isinstance(spec.get("stream"), str) and spec["stream"].endswith("_monitor"):
                    spec["stream"] = rename_column(types, spec["stream"][:-8]) + "_monitor"
            if new_specs != specs or new_plan != plan:
                log.append(f"layout {plan}" + (f" -> {new_plan}" if new_plan != plan else "") + ": columns renamed")
            out[new_plan] = new_specs
        _write(f, out, apply and data != out)
    f = root / "views.json"
    if f.exists():
        data = json.loads(f.read_text(encoding="utf-8"))
        out = json.loads(json.dumps(data))
        for dev, view in (out.get("devices") or {}).items():
            for g in view.get("graphs", []):
                new = [rename_pv(types.get(dev, ""), s) for s in g.get("pvs", [])]
                if new != g.get("pvs"):
                    log.append(f"view {dev}/{g.get('title')}: {g['pvs']} -> {new}")
                    g["pvs"] = new
        _write(f, out, apply and data != out)
    f = root / "derived.json"
    if f.exists():
        data = json.loads(f.read_text(encoding="utf-8"))
        out = json.loads(json.dumps(data))
        for d in out.get("values", []):
            for k, ref in (d.get("inputs") or {}).items():
                new = rename_ref(types, ref)
                if new != ref:
                    log.append(f"derived {d['name']}: {k} = {ref} -> {new}")
                    d["inputs"][k] = new
        _write(f, out, apply and data != out)
    f = root / "history" / "history.db"
    if f.exists():
        prefixes = _prefixes(lab)
        db = sqlite3.connect(f)
        try:
            moves = []
            for cid, pv, dev in db.execute("SELECT id, pv, device FROM channels").fetchall():
                prefix = prefixes.get(dev)
                if prefix and pv.startswith(prefix):
                    new = prefix + rename_pv(types.get(dev, ""), pv[len(prefix):])
                    if new != pv:
                        moves.append((cid, pv, new))
                        log.append(f"history {pv} -> {new}")
            if apply and moves:
                shutil.copy2(f, f.with_suffix(".db.bak"))
                with db:  # two phases: AuxOut0 -> 1 must not meet the old AuxOut1 on its way to 2
                    for cid, pv, new in moves:
                        db.execute("UPDATE channels SET pv = ? WHERE id = ?", ("__migrating__" + new, cid))
                    for cid, pv, new in moves:
                        hit = db.execute("SELECT id FROM channels WHERE pv = ?", (new,)).fetchone()
                        if hit:  # recorded under the new name already (IOC restarted first): move the rows over
                            for table in ("samples", "minutes"):
                                db.execute(f"UPDATE OR IGNORE {table} SET ch = ? WHERE ch = ?", (hit[0], cid))
                                db.execute(f"DELETE FROM {table} WHERE ch = ?", (cid,))
                            db.execute("DELETE FROM channels WHERE id = ?", (cid,))
                        else:
                            db.execute("UPDATE channels SET pv = ? WHERE id = ?", (new, cid))
        finally:
            db.close()
    return log


def _prefixes(lab: dict) -> dict:
    try:
        from fastq.discovery import device_name, read_directory

        return {device_name(e["prefix"]): e["prefix"] for e in read_directory()}
    except Exception:  # noqa: BLE001
        return {}


def main() -> None:
    from fastq.session import load_lab

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lab", help="lab file (default env FASTQ_LAB)")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: only show them)")
    ap.add_argument("--types", default="", help="device types if the directory is not reachable: itc1=itc503,...")
    a = ap.parse_args()
    lab = load_lab(a.lab)
    types = device_types(lab)
    types.update(dict(x.split("=", 1) for x in a.types.split(",") if "=" in x))
    log = migrate(lab, types, a.apply)
    print("\n".join(log) or "nothing to migrate")
    if log and not a.apply:
        print("\n(dry run - add --apply to write; the old files are kept as *.bak)")


if __name__ == "__main__":
    main()
