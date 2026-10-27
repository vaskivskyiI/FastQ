"""Tests of the declarative-instrument framework, without hardware and without
the simulator stack: each test starts the mock IOCs it needs (prefixes TEST:PT*).

    python -m pytest tests/test_framework.py -q
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "control"))

import ophyd  # noqa: E402

ophyd.set_cl("caproto")

from bluesky import RunEngine  # noqa: E402
from bluesky import plans as bp  # noqa: E402
from ophyd.sim import SynAxis  # noqa: E402

from fastq.detectors import remember_run  # noqa: E402
from fastq.generic import device_from_prefix  # noqa: E402
from fastq.plans.scan import nd_scan  # noqa: E402

IOCS = ROOT / "ioc_host" / "iocs"
DECLARATIVE = sorted(p for p in IOCS.glob("*_ioc.py") if "run_instrument(" in p.read_text(encoding="utf-8"))


def start_ioc(script: str, prefix: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-u", str(IOCS / f"{script}.py"), "--prefix", prefix, *args],
                            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def ioc(request):
    procs = []

    def _start(script, prefix, *args, wait=3.0):
        procs.append(start_ioc(script, prefix, *args))
        time.sleep(wait)
        dev = device_from_prefix(prefix, name=prefix.split(":")[1].lower())
        dev.wait_for_connection(timeout=10)
        return dev

    yield _start
    for p in procs:
        p.terminate()
        p.wait(10)


@pytest.mark.parametrize("path", DECLARATIVE, ids=[p.stem for p in DECLARATIVE])
def test_schema(path):
    """Every declarative IOC describes itself: type = file name, documented params."""
    out = subprocess.run([sys.executable, str(path), "--print-schema"], cwd=ROOT, capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    schema = json.loads(out.stdout)
    assert schema["type"] == path.stem.removesuffix("_ioc")
    assert schema["description"]
    assert schema["params"] or schema["actions"]


def test_positioner_and_refused_put(ioc, tmp_path):
    flow = ioc("voegtlin_ioc", "TEST:PTFLOW:", "--mock")
    flow.set(1.0).wait(60)                                       # set-and-confirm on the readback
    assert abs(flow.position - 1.0) <= 0.01
    t0 = time.monotonic()
    with pytest.raises(Exception, match="refused|outside limits"):
        flow.setpoint.set(500).wait(30)                          # beyond the declared limits
    assert time.monotonic() - t0 < 5                             # fails at once, not at the timeout
    flow.close_valve_cmd.set(1).wait(10)
    assert flow.control.get() == "Valve closed"


def test_camera_run_assets(ioc, tmp_path):
    cam = ioc("sim_camera_ioc", "TEST:PTCAM:", "--data-root", str(tmp_path), "--width", "256", "--height", "4")
    cam.rois.set(json.dumps([{"name": "A", "x": [10, 20]}, {"name": "B", "x": [100, 120], "bg": [0, 5]}])).wait(5)
    RE, docs = RunEngine(), []
    RE.subscribe(remember_run)
    RE(bp.count([cam], 3), lambda n, d: docs.append((n, d)))
    names = [n for n, _ in docs]
    assert names.count("stream_resource") == 1 and names.count("stream_datum") == 3
    event = next(d for n, d in docs if n == "event")
    assert {"ptcam_A", "ptcam_B"} <= set(event["data"])          # one scalar column per ROI
    assert not any(isinstance(v, list) for v in event["data"].values())
    sres = next(d for n, d in docs if n == "stream_resource")
    import h5py

    path = Path(sres["uri"].replace("file://localhost/", ""))
    with h5py.File(path, "r") as f:
        assert f["entry/data/data"].shape == (3, 4, 256)


def test_nd_scan_order():
    """Snake, repeat and re-applying inner axes after an outer move (no IOCs needed)."""
    outer, inner, flag = SynAxis(name="outer"), SynAxis(name="inner"), SynAxis(name="flag")
    RE, docs = RunEngine(), []
    RE(nd_scan([{"repeat": 2}, {"device": outer, "values": [0, 1]},
                {"device": inner, "values": [10, 20, 30], "snake": True},
                {"device": flag, "values": [5]}]), lambda n, d: docs.append((n, d)))
    events = [d["data"] for n, d in docs if n == "event"]
    assert len(events) == 2 * 2 * 3
    assert [e["inner"] for e in events[:6]] == [10, 20, 30, 30, 20, 10]   # snake on the second outer pass
    assert [e["repeat"] for e in events] == [0] * 6 + [1] * 6
    stop = next(d for n, d in docs if n == "stop")
    assert stop["exit_status"] == "success"
    start = next(d for n, d in docs if n == "start")
    assert start["shape"] == [2, 2, 3, 1] and json.dumps(start["plan_args"])  # md is JSON-safe


def test_plan_comment_parameter(tmp_path):
    import inspect

    from fastq.plan_registry import load_plans

    plans = load_plans(_lab(tmp_path))          # the code archive goes to this lab's data folder only
    assert {"fdm_delay_scan", "fdm_static"} <= set(plans)
    assert "comment" in inspect.signature(plans["fdm_delay_scan"]).parameters
    assert list((tmp_path / "code").glob("*.zip"))


LEGACY_PARAMS = {"itc", "excitation_current_a", "wait_s", "temperatures", "delays", "currents", "angles", "label",
                 "index", "pulse_width_s", "settle_temperature_s", "write_durations_fs", "write_attenuators_percent",
                 "nd_positions", "lockin_too", "rates_v_per_s", "table"}


def test_plan_conventions(tmp_path):
    """Every queueable plan: role-named device parameters, unit suffixes (no legacy names)."""
    import inspect

    from fastq.plan_registry import load_plans

    for name, plan in load_plans(_lab(tmp_path)).items():
        params = set(inspect.signature(plan).parameters)
        assert not params & LEGACY_PARAMS, f"{name}: {sorted(params & LEGACY_PARAMS)}"


def test_recipes_match_plans(tmp_path):
    """presets.yaml (public + private experiment folders): each recipe's plan exists and takes its
    arguments, and so does every step of a sequence recipe."""
    import inspect

    import yaml

    from fastq.plan_registry import experiment_dirs, load_plans

    plans = load_plans(_lab(tmp_path))
    files = [d / "presets.yaml" for d in experiment_dirs() if (d / "presets.yaml").exists()]
    assert files and files[0].parent == ROOT / "control" / "experiments"
    for f in files:
        recipes = yaml.safe_load(f.read_text(encoding="utf-8"))
        assert recipes, f
        for plan, ps in recipes.items():
            assert plan in plans, (f, plan)
            for name, r in ps.items():
                inspect.signature(plans[plan]).bind(**r["kwargs"])   # TypeError = a stale recipe
                assert r.get("setup") and r.get("comment"), name
                for step in r["kwargs"].get("steps", []) if plan == "sequence" else []:
                    inspect.signature(plans[step["plan"]]).bind(**step.get("kwargs", {}))


def test_dashboard_endpoints_exist(tmp_path):
    """Every /api path the web pages call is a route of the server (no stale calls after a rename)."""
    import re

    sys.path.insert(0, str(ROOT / "control" / "web"))
    from server import create_app

    app = create_app(_lab(tmp_path), read_only=True)
    routes = [re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", r.path) + "$") for r in app.routes if hasattr(r, "path")]
    called = set()
    for js in (ROOT / "control" / "web" / "static").glob("*.js"):
        for m in re.finditer(r"[`'\"](/(?:api|ws)/[^`'\"?]*)", js.read_text(encoding="utf-8")):
            called.add(re.sub(r"\$\{[^}]*\}", "x", m.group(1)).rstrip("/"))
    missing = sorted(c for c in called if not any(r.match(c) for r in routes))
    assert len(called) > 20 and not missing, missing


def test_plot_model():
    """fastq.analysis.plot_data: grouping, averaging over repeats, pumped - unpumped, maps."""
    import numpy as np
    import pandas as pd

    from fastq.analysis import plot_data

    rows = [{"repeat": r, "delay": d, "pump": p, "sig": 1.0 + d * p + 0.01 * r}
            for r in range(3) for d in (-1.0, 0.0, 1.0, 2.0) for p in (0, 1)]
    df = pd.DataFrame(rows)
    avg = plot_data(df, {"x": "delay", "y": ["sig"], "group": "pump", "average": True})
    assert [s["name"] for s in avg["series"]] == ["sig [pump=0]", "sig [pump=1]"]
    assert avg["series"][1]["y"] == pytest.approx([0.01, 1.01, 2.01, 3.01])      # mean over the 3 repeats
    assert all(e > 0 for e in avg["series"][0]["err"])                           # s.e.m. of the repeats
    diff = plot_data(df, {"x": "delay", "y": ["s*"], "group": "pump", "combine": "diff"})
    assert diff["series"][0]["y"] == pytest.approx([-1.0, 0.0, 1.0, 2.0])         # pumped - unpumped = delay
    heat = plot_data(df, {"kind": "heatmap", "x": "delay", "y": ["repeat"], "z": "sig"})
    assert np.array(heat["z"]).shape == (3, 4)


# ---------------------------------------------------------------- control-host services (no IOCs)
def _lab(tmp_path) -> dict:
    return {"_path": str(tmp_path / "lab.yaml"), "data": {"backup_dir": str(tmp_path / "jsonl")}}


def test_derived_values(tmp_path):
    import numpy as np
    import pandas as pd

    from fastq import derived as D

    lab = _lab(tmp_path)
    defs = D.save(lab, [{"name": "RSample", "units": "Ohm", "expr": "x / ((amp / sqrt(2) - x) / series_r)",
                         "inputs": {"x": "mfli1.x", "amp": "mfli1.amplitude"}, "constants": {"series_r": 1e6}}])
    assert D.load(lab) == defs and D.constant(lab, "series_r") == 1e6
    for bad in ({"name": "R", "expr": "open(x)", "inputs": {"x": "a.b"}},
                {"name": "R", "expr": "x.__class__", "inputs": {"x": "a.b"}},
                {"name": "R", "expr": "y * 2", "inputs": {"x": "a.b"}},
                {"name": "lower", "expr": "1"}):
        with pytest.raises((ValueError, SyntaxError)):
            D.validate([bad])
    # per row, the amplitude from another stream interpolated in time
    d = {**defs[0], "keys": {"x": "mfli1_x", "amp": "mfli1_amplitude"}}
    df = pd.DataFrame({"time": [0.0, 1.0, 2.0], "mfli1_x": [1e-3, 1e-3, 2e-3]})
    amp = (np.array([0.0, 2.0]), np.array([1.0, 1.0]))
    D.add_columns(df, [d], {"RSample"}, other=lambda key: amp if key == "mfli1_amplitude" else None)
    r = 1e-3 / ((1 / np.sqrt(2) - 1e-3) / 1e6)
    assert df["RSample"][0] == pytest.approx(r)


def test_history_recorder(tmp_path):
    from fastq import history as H

    lab = _lab(tmp_path)
    H.save_settings(lab, {"default": {"period_s": 1, "raw_days": 1, "keep_days": 30}, "devices": {"dev": {"period_s": 5}}})
    rec = H.Recorder(lab)
    try:
        rec.channel("P:A", "dev", "K")
        t0 = 1_800_000_000.0
        for i in range(120):                    # 2 minutes at 1 Hz
            rec.add("P:A", float(i), t0 + i)
        rec.add("P:A", 0.0, t0 + 200)           # closes the last minute
        time.sleep(H.FLUSH_S + 1.5)
        raw = rec.query("P:A", t0, t0 + 130, points=1000)
        assert raw["source"] == "raw" and len(raw["t"]) == 24     # one per 5 s (the device's period)
        env = rec.query("P:A", t0 - 86400, t0 + 86400, points=10)   # span beyond the raw rows: minute envelopes
        assert env["source"] == "minutes" and min(env["vmin"]) == 0 and max(env["vmax"]) == 119
    finally:
        rec.stop()


def test_pin_check():
    from fastq import security as SEC

    lab = {"security": {"pin_sha256": SEC.hash_pin("4321"), "overrides": {"laser1.ShutterOpen": "protected"}}}
    SEC._fails.clear()
    SEC.check_pin(lab, "4321")
    for wrong in (None, "0000"):
        with pytest.raises(PermissionError):
            SEC.check_pin(lab, wrong)
    assert SEC.level(lab, "laser1", "ShutterOpen", "confirm") == "protected"
    assert SEC.level(lab, "laser1", "Attenuator", "safe") == "safe"
    with pytest.raises(PermissionError):
        SEC.check_pin({}, "4321")                  # no PIN configured: protected controls refused
    for _ in range(SEC.LOCKOUT_AFTER):
        with pytest.raises(PermissionError):
            SEC.check_pin(lab, "1")
    with pytest.raises(PermissionError, match="locked"):
        SEC.check_pin(lab, "4321")                 # locked even with the right PIN
    SEC._fails.clear()


def test_prefixes_and_controls():
    sys.path.insert(0, str(ROOT))
    from fastq.profiles import profile_from_schema
    from ioc_host.common.naming import device_name, prefixes

    assert prefixes('--camera-index 1 --prefix FDM:CAMMAIN: --also "--camera-index 2 --prefix FDM:CAMREF:"') == \
        ["FDM:CAMMAIN:", "FDM:CAMREF:"]
    assert prefixes("--prefix=A:B:") == ["A:B:"] and device_name("FDM:CAMMAIN:") == "cammain"
    schema = json.loads(subprocess.run([sys.executable, str(IOCS / "carbide_ioc.py"), "--print-schema"],
                                       capture_output=True, text=True, cwd=ROOT).stdout)
    controls = {c["write_suffix"]: c for c in profile_from_schema(schema)["controls"]}
    assert controls and all(c.get("safety") in ("safe", "confirm", "protected") for c in controls.values())


def test_migration_of_old_names(tmp_path):
    """fastq.migrate: old plan requests, presets and derived inputs come out with today's names."""
    import inspect

    from fastq import migrate as MG
    from fastq.plan_registry import load_plans

    plans = load_plans(_lab(tmp_path))
    cases = [("tm_dc_iv", {"settle_s": 0.5, "temperatures": [3]}), ("temperature_log", {"duration_s": 10}),
             ("aux_waveform", {"index": 0, "start_v": 1, "stop_v": 0, "rate_v_per_s": 0.1}),
             ("tm_rt_steps", {"temperatures": [10], "wait_s": 5, "lockin_too": True}),
             ("fdm_delay_scan", {"delays": [0, 1], "repeats": 2}), ("iv_curves", {"settle_s": 1.0, "settle_temperature_s": 30})]
    for name, kw in cases:
        new, kwargs, notes = MG.translate_request(name, kw)
        inspect.signature(plans[new]).bind(**kwargs)      # binds to today's plan
    assert MG.translate_request("aux_waveform", {"index": 0})[1]["aux_index"] == 1
    assert MG.translate_request("iv_curves", {"settle_s": 1.0, "settle_temperature_s": 30})[1] == \
        {"first_settle_s": 1.0, "settle_s": 30}
    types = {"mfli1": "mfli", "lockin": "ametek_lockin"}
    assert MG.rename_ref(types, "mfli1.sig_out_amp") == "mfli1.amplitude"
    assert MG.rename_column(types, "mfli1_aux_out0") == "mfli1_aux_out1"
    assert MG.rename_pv("mfli", "AuxOut3_RBV") == "AuxOut4_RBV" and MG.rename_pv("ametek_lockin", "DAC2") == "AuxOut2"
    lab = _lab(tmp_path)
    (tmp_path / "presets.json").write_text(json.dumps({"rt_sweep": {"cool": {"kwargs": {"itc": "itc1"}}}}))
    (tmp_path / "derived.json").write_text(json.dumps({"values": [{"name": "R", "expr": "x", "inputs": {
        "x": "mfli1.sig_out_amp"}, "constants": {}}]}))
    MG.migrate(lab, types, apply=True)
    assert json.loads((tmp_path / "presets.json").read_text())["rt_sweep"]["cool"]["kwargs"]["temperature"] == "itc1"
    assert json.loads((tmp_path / "derived.json").read_text())["values"][0]["inputs"]["x"] == "mfli1.amplitude"
    assert (tmp_path / "presets.json.bak").exists()


def test_accounts_roles_and_audit(tmp_path):
    """fastq.users through the web server: open without accounts, login, roles, audit log."""
    from fastapi.testclient import TestClient

    sys.path.insert(0, str(ROOT / "control" / "web"))
    from fastq import users as U
    from server import create_app

    lab = _lab(tmp_path)
    c = TestClient(create_app(lab))                       # no `with`: no queue server / history thread
    assert c.get("/api/me").json() == {"user": {"name": "lab", "role": "admin", "display": "lab"}, "accounts": False}
    U.set_user(lab, "alice", "alice-pass-1", "admin")
    U.set_user(lab, "vic", "viewer-pass-1", "viewer")
    with pytest.raises(ValueError):
        U.set_user(lab, "alice", role="viewer")           # the last admin stays
    assert c.get("/api/users").status_code == 401         # accounts now: login required
    assert c.post("/api/login", json={"user": "alice", "password": "nope"}).status_code == 401
    v = TestClient(c.app)
    assert v.post("/api/login", json={"user": "vic", "password": "viewer-pass-1"}).status_code == 200
    assert v.get("/api/users").status_code == 200        # viewers read
    r = v.put("/api/presets/nd_scan/x", json={"kwargs": {}})
    assert r.status_code == 403 and "needs operator" in r.json()["detail"]
    assert c.post("/api/login", json={"user": "alice", "password": "alice-pass-1"}).status_code == 200
    assert c.put("/api/presets/nd_scan/x", json={"kwargs": {}}).status_code == 200
    assert v.put("/api/users/bob", json={"password": "bob-pass-12", "role": "operator"}).status_code == 403
    assert c.put("/api/users/bob", json={"password": "bob-pass-12", "role": "operator"}).json()["role"] == "operator"
    log = U.audit_tail(lab)
    assert {"login", "PUT /api/presets/nd_scan/x", "PUT /api/users/bob"} <= {r["what"] for r in log}
    assert any(r["what"] == "login" and not r["ok"] for r in log)


def test_alarm_rules():
    from fastq import alarms as AL

    events = []
    rules = AL.validate([{"name": "hot", "ref": "itc1.temp", "above": 10, "for_s": 30, "actions": ["notify"]},
                         {"name": "drift", "ref": "itc1.temp", "rate_per_min": 1.0, "for_s": 0}])
    eng = AL.Engine(rules, events.append)
    for t, v in [(0, 9), (10, 11), (20, 11), (35, 11)]:   # above from t=10, active only after 30 s
        eng.update("itc1.temp", v, t)
    assert [e["kind"] for e in events if e["rule"]["name"] == "hot"] == []
    eng.update("itc1.temp", 11, 41)
    assert [e["kind"] for e in events if e["rule"]["name"] == "hot"] == ["active"]
    for t in (50, 60, 66):                                # cleared after for_s / 2 below the limit
        eng.update("itc1.temp", 9, t)
    assert [e["kind"] for e in events if e["rule"]["name"] == "hot"] == ["active", "cleared"]
    eng2 = AL.Engine(AL.validate([{"name": "drift", "ref": "x.y", "rate_per_min": 1.0, "for_s": 0}]), events.append)
    for t in range(0, 61, 10):
        eng2.update("x.y", t / 60 * 3.0, t)              # 3 K/min
    assert any(e["rule"]["name"] == "drift" and e["kind"] == "active" for e in events)
    with pytest.raises(ValueError):
        AL.validate([{"name": "x", "ref": "nodot", "above": 1}])


def test_config_examples_and_local_copy(tmp_path, monkeypatch):
    """Every tracked example loads; the per-PC files are git-ignored and made from an example."""
    import importlib.util

    import yaml
    from fastq.session import backup_dir, load_lab

    sys.path.insert(0, str(ROOT / "ioc_host"))
    from supervisor.config import load_config

    hosts = sorted((ROOT / "ioc_host" / "hosts").glob("*.example.yaml"))
    labs = sorted((ROOT / "control").glob("lab*.example.yaml"))
    assert len(hosts) >= 3 and len(labs) >= 3
    for p in hosts:
        cfg = load_config(p)
        assert "directory_ioc" in cfg.devices, p
        assert all(d.ioc_script and (IOCS / f"{d.ioc_script}.py").exists() for d in cfg.devices.values()), p
    for p in labs:
        lab = load_lab(p)
        assert lab["directory"] and backup_dir(lab), p
    ignored = subprocess.run(["git", "check-ignore", "control/lab.yaml", "ioc_host/supervisor_config.yaml",
                              "control/lab_sim.yaml", "ioc_host/hosts/4dmag.example.yaml"],
                             cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert ignored == ["control/lab.yaml", "ioc_host/supervisor_config.yaml"]

    spec = importlib.util.spec_from_file_location("local_config", ROOT / "tools" / "local_config.py")
    lc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lc)
    target = tmp_path / "control" / "lab.yaml"
    target.parent.mkdir()
    monkeypatch.setitem(lc.KINDS, "lab", (target, ROOT / "control", "lab*.example.yaml"))
    monkeypatch.setattr(lc, "ROOT", tmp_path)
    example = ROOT / "control" / "lab.4dmag.example.yaml"
    monkeypatch.setenv("FASTQ_CONFIG_EXAMPLE", str(example))
    assert lc.main(["lab", str(target)]) == 2 and target.read_bytes() == example.read_bytes()
    target.write_text("directory: X\n", encoding="utf-8")       # local edits survive the next start
    assert lc.main(["lab", str(target)]) == 0 and yaml.safe_load(target.read_text())["directory"] == "X"
