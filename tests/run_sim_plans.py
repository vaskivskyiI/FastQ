"""Run every measurement plan against the simulated IOC host (sim/sim_stack.py)
with short parameters and check the resulting documents.

    python sim/sim_stack.py                  (other terminal)
    python tests/run_sim_plans.py
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "control"))
os.environ.setdefault("FASTQ_LAB", str(ROOT / "control" / "lab_sim.yaml"))

import fastq.startup as S  # noqa: E402

results = []


def run(name, plan, check):
    docs = []
    t0 = time.time()
    try:
        S.RE(plan, lambda n, d: docs.append((n, d)))
        stop = next(d for n, d in docs if n == "stop")
        events = Counter()
        desc = {d["uid"]: d["name"] for n, d in docs if n == "descriptor"}
        for n, d in docs:
            if n == "event":
                events[desc[d["descriptor"]]] += 1
        ok, detail = check(stop, events, docs)
        ok = ok and stop["exit_status"] == "success"
    except Exception as exc:
        traceback.print_exc()
        ok, detail, events = False, f"{type(exc).__name__}: {exc}", {}
    results.append((name, ok, f"{time.time() - t0:.0f}s {detail}"))
    print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}  events={dict(events)}", flush=True)


def events_of(docs, stream):
    desc = {d["uid"] for n, d in docs if n == "descriptor" and d["name"] == stream}
    return [d for n, d in docs if n == "event" and d["descriptor"] in desc]


itc, mfli, laser, k = S.role("temperature"), S.role("lockin"), S.role("laser"), S.role("keithley")

t0 = round(itc.position, 2)
run("temperature log (nd_scan, period_s)", S.nd_scan([{"repeat": 6}], readables=["@temperature"], period_s=0.5),
    lambda s, e, d: (e["primary"] == 6 and d[0][1]["run_kind"] == "Scan", f"{e['primary']} readings"))

run("rt_sweep (2 segments)", S.rt_sweep(itc, ["@lockin"], segments=[[t0, 9.0, 1.0], [9.0, 9.5, 1.0]],
                                        period_s=0.5, current_a=1e-6),
    lambda s, e, d: (e["primary"] > 3 and e["mfli1_x_monitor"] > 10 and "RSample" in str(d[0][1].get("derived"))
                     and d[0][1]["plot_hints"]["signals"][0] == "RSample",
                     f"X~{events_of(d, 'primary')[-1]['data']['mfli1_x']:.3g} V, hints {d[0][1]['plot_hints']}"))

run("rt_sweep (Lake Shore ramp interface)", S.rt_sweep("ls336", ["@lockin"], segments=[[round(S.devices['ls336'].position, 2),
                                                                                        round(S.devices['ls336'].position, 2) + 0.3, 6.0]],
                                                         period_s=0.5, go_to_start=False),
    lambda s, e, d: (e["primary"] >= 1, f"{e['primary']} points"))

run("iv_curves", S.iv_curves(max_current_a=1e-5, points_per_quadrant=3),
    lambda s, e, d: (e["iv"] == 9, f"{e['iv']} I-V points"))

run("tm_rt_continuous (time_min, nplc)", S.tm_rt_continuous(start_k=9.5, final_k=9.3, time_min=5, count=10, nplc=0.1,
                                                          rate_k_per_min=None),
    lambda s, e, d: (e["primary"] >= 1 and d[0][1]["run_kind"] == "R(T) continuous", f"{e['primary']} delta measurements"))

run("laser_sequence_scan (pulses + energy ramp, 2 durations)", S.laser_sequence_scan(
    sequence=[{"kind": "reference", "duration_s": 0.5, "phase": "before"},
              {"kind": "pulses", "duration_fs": "$pulse_duration_fs", "attenuator_percent": 2, "pulse_count": 2,
               "pulse_delay_s": 0.3},
              {"kind": "ramp", "duration_fs": "$pulse_duration_fs", "start_percent": 3, "stop_percent": 0,
               "divider": 1000, "rate_v_per_s": 0.5}],
    dimensions={"pulse_duration_fs": [221, 500]}, laser_limits={"max_attenuator_percent": 5, "min_pp_divider": 100}),
    lambda s, e, d: (e["condition_monitor"] == 2 and laser.shutter_open.get() in (0, "Closed", False),
                     f"phases: {sorted({x['data']['phase'] for x in events_of(d, 'phase_monitor')})}"))

run("aux_waveform (AuxOut1 ramp)", S.aux_waveform(aux_index=1, start_v=0.5, stop_v=0.0, rate_v_per_s=0.5),
    lambda s, e, d: (e["mfli1_aux_out1_monitor"] > 5, "ramp recorded"))

# ---- transport recipes (TransportMeasure)
run("tm_delta", S.tm_delta(points=3, count=5),
    lambda s, e, d: (e["primary"] == 3, f"R~{events_of(d, 'primary')[-1]['data']['resistance']:.0f} Ohm"))
run("tm_pulse_delta", S.tm_pulse_delta(points=2, count=4), lambda s, e, d: (e["primary"] == 2, "ok"))
run("tm_pulse_sweep", S.tm_pulse_sweep(start_a=0.0, stop_a=1e-4, step_a=2.5e-5),
    lambda s, e, d: (e["iv"] >= 5, f"{e['iv']} pulses"))
run("tm_single_pulse", S.tm_single_pulse(), lambda s, e, d: (e["primary"] == 1, "ok"))
run("tm_rt_steps (2 T)", S.tm_rt_steps(temperatures_k=[round(itc.position, 2), round(itc.position, 2) + 0.2],
                                       tolerance_k=0.2, settle_s=1, count=4),
    lambda s, e, d: (e["primary"] == 2, f"keithley after: {k.state.get()}"))

# ---- 4DMAG recipes (cameras, pump pulser, delay line)
run("fdm_static (pump on/off)", S.fdm_static(repeats=2, pump_values=[0, 1]),
    lambda s, e, d: (e["primary"] == 4 and d[0][1]["plot_hints"]["group"] == "pump_shutter",
                     f"pump after run: {S.role('pulser').shutter.get()}"))
run("fdm_delay_scan", S.fdm_delay_scan(delays_ps=[0.0, 1.0, 2.0], repeats=1, pump_values=[0, 1]),
    lambda s, e, d: (e["primary"] == 6, "ok"))
run("fdm_background", S.fdm_background(repeats=2), lambda s, e, d: (e["primary"] == 2, "ok"))
run("nd_scan (builder style)", S.nd_scan([{"device": "waveplate", "values": [0, 10]}, {"repeat": 2}],
                                         detectors=["cammain"], safe_state=[["pump.shutter", 0]]),
    lambda s, e, d: (e["primary"] == 4, "ok"))

# ---- queue flow (unattended nights)
t_now = itc.position
run("log_until (temperature below now + 5 K)", S.log_until("@temperature", below=t_now + 5, for_s=1, readables=["@lockin"],
                                                          period_s=0.5),
    lambda s, e, d: (e["primary"] >= 2 and d[0][1]["run_kind"] == "log_until", f"{e['primary']} points"))

run("nd_scan lock-in frequency sweep (recipe form)", S.nd_scan(
    axes=[{"device": "@lockin", "signal": "frequency", "settle_s": 0.2, "values": [100, 1000, 5000]}],
    readables=["@lockin", "@temperature"]),
    lambda s, e, d: (e["primary"] == 3, f"{e['primary']} frequencies"))

run("sequence (wait_for + tm_delta, 2 passes)", S.sequence(
    steps=[{"plan": "wait_for", "kwargs": {"signal": "@temperature", "within": [t_now, 1.0], "for_s": 1}},
           {"plan": "tm_delta", "kwargs": {"points": 2, "count": 3}, "comment": "flow test"}], repeats=2),
    lambda s, e, d: (sum(1 for n, x in d if n == "start") == 2 and d[0][1].get("comment") == "flow test"
                     and d[0][1]["plan_name"] == "tm_delta" and d[0][1].get("part_of") == "sequence",
                     f"{sum(1 for n, x in d if n == 'start')} runs"))

# ---- private plans (not in the public repository): ../FastQ-private/tests/sim_cases.py, same helpers
PRIVATE_CASES = ROOT.parent / "FastQ-private" / "tests" / "sim_cases.py"
if PRIVATE_CASES.exists():
    exec(compile(PRIVATE_CASES.read_text(encoding="utf-8"), str(PRIVATE_CASES), "exec"))

print(f"\n{sum(ok for _, ok, _ in results)}/{len(results)} passed")
print("laser after tests: shutter", laser.shutter_open.get(), "pp", laser.pp_enable.get(), "energy", laser.energy_control.get())
os._exit(0 if all(ok for _, ok, _ in results) else 1)
