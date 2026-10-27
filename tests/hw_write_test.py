"""WRITE tests against the REAL instruments (run on transport PC), approved
by the user 2026-09-26:
  * Keithley: Delta +-1 uA / 20 ms / 10 points / 10 V compliance, an Abort
    test, PulseDelta 10 pulses at 1 uA, DC 1 uA x 5 readings. Sources
    current through the connected sample; output off afterwards.
  * MFLI: no-op writes (write each current value back).
  * Carbide: no-op writes in writable mode (attenuator = current value,
    shutter CLOSE while already closed). NEVER opens the shutter.

    .venv\\Scripts\\python.exe tests\\hw_write_test.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from hw_readonly_test import CA, RESULTS, check, snapshot, wait_connected  # noqa: E402


class Args:
    itc = "GPIB0::24::INSTR"
    keithley = "GPIB0::18::INSTR"
    mfli_host = "192.168.1.94"
    mfli_device = "dev1234"
    demod = 1
    laser_host = "192.168.1.10"


def keithley_tests(ca, a):
    P = "HWTEST:K:"

    def run(mode, timeout=120, **params):
        ca.put(P + "Mode", mode)
        for k, v in params.items():
            ca.put(P + k, v)
        t0 = time.time()
        ca.pv(P + "Acquire").write([1], wait=True, timeout=timeout)
        r = {s: ca.get(P + s) for s in ("State", "Message", "N", "Mean", "StdDev", "PointsDone", "PointsTotal")}
        r["dt"] = round(time.time() - t0, 1)
        r["Readings"] = ca.get(P + "Readings")
        return r

    common = dict(Compliance=10.0, CAB=1)
    r = run("Delta", DeltaHigh=1e-6, DeltaLow=-1e-6, DeltaDelay=0.02, DeltaCount=10, **common)
    readings = r.pop("Readings")
    check("Keithley Delta 10 pts", r["State"] == "Done" and r["N"] == 10, json.dumps(r, default=str))
    print("        readings:", [f"{x:.4e}" for x in (readings if isinstance(readings, list) else [readings])])

    # Abort mid-run
    ca.put(P + "DeltaCount", 300)
    ca.pv(P + "Acquire").write([1], wait=False)
    for _ in range(40):  # abort only once it is really running
        if ca.get(P + "State") == "Running" and ca.get(P + "PointsDone") >= 5:
            break
        time.sleep(0.25)
    done_before = ca.get(P + "PointsDone")
    ca.put(P + "Abort", 1)
    for _ in range(40):  # wait for the run to wind down before the next Acquire
        if ca.get(P + "Busy") == "Off":
            break
        time.sleep(0.25)
    st, n = ca.get(P + "State"), ca.get(P + "N")
    check("Keithley Abort (while running) keeps partial data", st == "Aborted" and n > 0,
          f"state={st} N={n} (PointsDone before abort={done_before})")

    # Abort during Configuring: must never start the source
    ca.pv(P + "Acquire").write([1], wait=False)
    time.sleep(0.3)
    ca.put(P + "Abort", 1)
    for _ in range(40):
        if ca.get(P + "Busy") == "Off":
            break
        time.sleep(0.25)
    check("Keithley Abort during Configuring", ca.get(P + "State") == "Aborted", ca.get(P + "Message"))

    r = run("PulseDelta", PdHigh=1e-6, PdLow=0.0, PdCount=10, PdIntervalPLC=5, PdLowMeas=2,
            PdWidth=110e-6, PdSourceDelay=60e-6, **common)
    readings = r.pop("Readings")
    check("Keithley PulseDelta 10 pts", r["State"] == "Done" and r["N"] == 10, json.dumps(r, default=str))

    r = run("DC", DcCurrent=1e-6, DcReadings=5, DcSettleS=0.5, NPLC=0.0, **common)
    readings = r.pop("Readings")
    check("Keithley DC 5 readings", r["State"] == "Done" and r["N"] == 5, json.dumps(r, default=str))
    print("        readings:", readings)


def mfli_tests(ca, a):
    P = "HWTEST:MFLI:"
    before = {s: ca.get(P + s) for s in ("AuxOut1_RBV", "AuxOut2_RBV", "AuxOut3_RBV", "AuxOut4_RBV", "Frequency_RBV")}
    # The MFLI data server has almost no free client slots (LabOne web UI
    # holds most), so no second API session while the IOC runs: take the
    # current values from the BEFORE snapshot (read before the IOC started).
    tc, freq = a.before_mfli["timeconstant"], a.before_mfli["osc0_freq"]
    for i in range(4):
        ca.put(P + f"AuxOut{i}", float(before[f"AuxOut{i}_RBV"]))
    ca.put(P + "Frequency", freq)
    ca.put(P + "TimeConstant", tc)
    time.sleep(1.0)
    after = {s: ca.get(P + s) for s in before}
    check("MFLI no-op writes accepted, AuxOut unchanged",
          all(abs(after[k] - before[k]) < 1e-6 for k in ("AuxOut1_RBV", "AuxOut2_RBV", "AuxOut3_RBV", "AuxOut4_RBV")), str(after))
    # tc/freq verified against the AFTER snapshot (taken once the IOC is stopped)


def laser_tests(ca, a):
    P = "HWTEST:LASER:"
    if ca.get(P + "ShutterOpen_RBV") != "Off":
        check("Laser tests", False, "shutter not closed - skipping all laser writes")
        return
    att = float(ca.get(P + "Attenuator_RBV"))
    ca.put(P + "Attenuator", att)
    ca.put(P + "ShutterOpen", 0)  # CLOSE (already closed) - never 1
    time.sleep(2.0)
    check("Laser no-op writes accepted, state unchanged",
          ca.get(P + "ShutterOpen_RBV") == "Off" and abs(float(ca.get(P + "Attenuator_RBV")) - att) < 0.01,
          f"shutter={ca.get(P + 'ShutterOpen_RBV')} att={ca.get(P + 'Attenuator_RBV')}")


def main():
    a = Args()
    print("=== snapshot BEFORE")
    before = snapshot(a)
    print(json.dumps(before, indent=1, default=str))
    a.before_mfli = before.get("mfli") if isinstance(before.get("mfli"), dict) else {}
    if isinstance(before.get("laser"), dict) and before["laser"]["ActualShutterState"] != "Closed":
        sys.exit("ABORT: laser shutter is not closed - not running any tests")

    py = sys.executable
    logdir = ROOT / "tests" / "hwtest_logs"
    logdir.mkdir(parents=True, exist_ok=True)
    devices = {
        "keithley": ("HWTEST:K:", ["ioc_host/iocs/keithley_ioc.py", "--resource", a.keithley,
                                   "--setup-command", "OUTPut:RESPonse SLOW", "--setup-command", "OUTP:ISH OLOW"],
                     keithley_tests),
        "mfli": ("HWTEST:MFLI:", ["ioc_host/iocs/mfli_ioc.py", "--device", a.mfli_device, "--labone-host", a.mfli_host,
                                  "--demod-index", str(a.demod)], mfli_tests),
        "laser": ("HWTEST:LASER:", ["ioc_host/iocs/carbide_ioc.py", "--host", a.laser_host], laser_tests),  # writable
    }
    procs = {}
    for name, (prefix, cmd, _) in devices.items():
        log = open(logdir / f"write_{name}.log", "w")
        procs[name] = (subprocess.Popen([py, "-u", *cmd, "--prefix", prefix], cwd=ROOT,
                                        stdout=log, stderr=subprocess.STDOUT), log)
        time.sleep(1.0)
    ca = CA()
    try:
        for name, (prefix, _, fn) in devices.items():
            if not check(f"{name} IOC Connected", wait_connected(ca, prefix)):
                continue
            try:
                fn(ca, a)
            except Exception as e:
                check(f"{name} tests", False, f"aborted: {type(e).__name__}: {e}")
    finally:
        try:
            ca.ctx.disconnect()
        except Exception:
            pass
        for name, (p, log) in procs.items():
            p.terminate()
            p.wait(10)
            log.close()

    time.sleep(2)
    print("\n=== snapshot AFTER")
    after = snapshot(a)
    for dev in before:
        same = before[dev] == after[dev]
        check(f"{dev} state after tests", same, "" if same else f"before={before[dev]} after={after[dev]}")
    k = after.get("keithley", {})
    check("Keithley output OFF at the end", isinstance(k, dict) and k.get("OUTP?") == "0", str(k))
    for f in sorted(logdir.glob("write_*.log")):
        bad = [l.strip() for l in f.read_text(errors="replace").splitlines() if "Traceback" in l or "| ERROR |" in l]
        check(f"log {f.name} clean", not bad, "; ".join(bad[:3]))
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n=== {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")


if __name__ == "__main__":
    main()
