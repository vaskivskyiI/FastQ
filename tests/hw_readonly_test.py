"""Read-only hardware test of all IOCs against the REAL instruments.

Runs on the lab PC (transport PC). Sends NO set commands to any instrument:
  1. snapshot every instrument's state directly (queries only)
  2. start all four IOCs together on this host (Carbide with --read-only),
     under a HWTEST: prefix so nothing clashes with production PVs
  3. over Channel Access: Connected, every readback PV, update rates,
     .SCAN fields, FastPoll (IOC-side only), Keithley idle state
  4. stop the IOCs, snapshot again, and diff: any change = FAIL

    .venv\\Scripts\\python.exe tests\\hw_readonly_test.py
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from caproto import ChannelType  # noqa: E402
from caproto.threading.client import Context  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)
    return ok


# ---------------------------------------------------------------- snapshots
def snapshot(a) -> dict:
    snap = {}
    import pyvisa
    from pymeasure.instruments.oxfordinstruments import ITC503

    try:
        itc = ITC503(a.itc)
        snap["itc"] = {k: getattr(itc, k) for k in ("control_mode", "heater_gas_mode", "temperature_setpoint", "sweep_status")}
        itc.adapter.connection.close()
    except Exception as e:
        snap["itc"] = f"ERR {e}"
    try:
        k = pyvisa.ResourceManager().open_resource(a.keithley)
        k.read_termination = k.write_termination = "\n"
        k.timeout = 5000
        snap["keithley"] = {q: k.query(q).strip() for q in (
            "OUTP?", "SOUR:CURR?", "SOUR:CURR:COMP?", "SOUR:DELT:HIGH?", "SOUR:DELT:LOW?",
            "SOUR:DELT:DEL?", "SOUR:DELT:ARM?", "SOUR:PDEL:ARM?")}
        k.close()
    except Exception as e:
        snap["keithley"] = f"ERR {e}"
    try:
        with urllib.request.urlopen(f"http://{a.laser_host}:20010/v1/Basic", timeout=5) as r:
            b = json.load(r)
        snap["laser"] = {k: b[k] for k in ("ActualShutterState", "IsOutputEnabled", "TargetAttenuatorPercentage",
                                           "TargetPulseDuration", "TargetPpDivider", "TargetBurstPulseCount")}
    except Exception as e:
        snap["laser"] = f"ERR {e}"
    try:
        import zhinst.core as zc

        d = zc.ziDAQServer(a.mfli_host, 8004, 6)
        dev, i = a.mfli_device, a.demod
        snap["mfli"] = {
            "demod_enable": d.getInt(f"/{dev}/demods/{i}/enable"),
            "timeconstant": round(d.getDouble(f"/{dev}/demods/{i}/timeconstant"), 9),
            "osc0_freq": round(d.getDouble(f"/{dev}/oscs/0/freq"), 6),
            **{f"auxout{j}": round(d.getDouble(f"/{dev}/auxouts/{j}/offset"), 9) for j in range(4)},
        }
        d.disconnect()  # free the slot: the MFLI data server has very few client slots

    except Exception as e:
        snap["mfli"] = f"ERR {e}"
    return snap


# ---------------------------------------------------------------- CA helpers
class CA:
    def __init__(self):
        self.ctx = Context()
        self.pvs = {}

    def pv(self, name):
        if name not in self.pvs:
            (p,) = self.ctx.get_pvs(name, timeout=3)
            p.wait_for_connection(timeout=5)
            self.pvs[name] = p
        return self.pvs[name]

    def get(self, name):
        p = self.pv(name)
        t = p.channel.native_data_type
        if t == ChannelType.ENUM:
            v = p.read(data_type=ChannelType.STRING, timeout=5).data[0]
            return v.decode() if isinstance(v, bytes) else v
        data = p.read(timeout=5).data
        if t == ChannelType.CHAR:
            return bytes(bytearray(data)).split(b"\0", 1)[0].decode(errors="replace")
        if t == ChannelType.STRING:
            v = data[0]
            return v.decode() if isinstance(v, bytes) else v
        return data[0] if len(data) == 1 else list(data)

    def put(self, name, value):
        p = self.pv(name)
        if isinstance(value, str) and p.channel.native_data_type == ChannelType.CHAR:
            p.write(list(value.encode()) + [0], wait=True, timeout=5)  # long string (char waveform)
        elif isinstance(value, str):
            p.write([value], data_type=ChannelType.STRING, wait=True, timeout=5)
        else:
            p.write([value], wait=True, timeout=5)

    def count_updates(self, name, seconds):
        hits = []
        cb = lambda sub, resp: hits.append(time.monotonic())  # noqa: E731
        sub = self.pv(name).subscribe(data_type="time")
        sub.add_callback(cb)
        time.sleep(seconds)
        sub.clear()
        return len(hits), cb


def wait_connected(ca, prefix, timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if ca.get(prefix + "Connected") in ("On", 1, True):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


# ---------------------------------------------------------------- device checks
def itc_checks(ca):
    P = "HWTEST:ITC:"
    vals = {s: ca.get(P + s) for s in ("Temp", "Temp2", "Temp3", "Setpoint_RBV", "Heater",
                                         "GasFlow", "SweepStep", "Ramping", "HeaterGasMode_RBV", "ControlMode_RBV")}
    check("ITC readbacks", all(v is not None for v in vals.values()), str(vals))
    check("ITC Temp plausible", 0 < float(vals["Temp"]) < 400, f"{vals['Temp']} K")
    n, _ = ca.count_updates(P + "Temp", 5)
    check("ITC Temp updates (1 s scan)", 3 <= n <= 8, f"{n} updates/5s")
    check("ITC Temp.SCAN shows real rate", ca.get(P + "Temp.SCAN") == "1 second", ca.get(P + "Temp.SCAN"))
    ca.put(P + "FastPollTarget", "Temp"); ca.put(P + "FastPollPeriodS", 0.2); ca.put(P + "FastPollDurationS", 4)
    ca.put(P + "FastPollActive", 1)
    time.sleep(1.2)
    n, _ = ca.count_updates(P + "Temp", 2)
    check("ITC FastPoll 0.2 s", n >= 6, f"{n} updates/2s")
    time.sleep(3)
    check("ITC FastPoll auto-revert", ca.get(P + "FastPollActive") == "Off", ca.get(P + "FastPollActive"))


def mfli_checks(ca):
    P = "HWTEST:MFLI:"
    vals = {s: ca.get(P + s) for s in ("X", "Y", "R", "Frequency_RBV", "AuxIn1", "AuxIn2",
                                         "AuxOut1_RBV", "AuxOut2_RBV", "AuxOut3_RBV", "AuxOut4_RBV")}
    check("MFLI readbacks", all(v is not None for v in vals.values()), str({k: f"{v:.4g}" for k, v in vals.items()}))
    n, _ = ca.count_updates(P + "X", 4)
    check("MFLI X updates (5 Hz)", 15 <= n <= 25, f"{n} updates/4s")
    check("MFLI X.SCAN", ca.get(P + "X.SCAN") == ".2 second", ca.get(P + "X.SCAN"))
    ca.put(P + "FastPollTarget", "X")  # two poll groups (sample, settings)
    ca.put(P + "FastPollPeriodS", 0.05); ca.put(P + "FastPollDurationS", 3); ca.put(P + "FastPollActive", 1)
    time.sleep(0.5)
    n, _ = ca.count_updates(P + "X", 2)
    check("MFLI FastPoll 0.05 s", n >= 25, f"{n} updates/2s")
    time.sleep(3)
    check("MFLI FastPoll auto-revert", ca.get(P + "FastPollActive") == "Off")


def laser_checks(ca):
    P = "HWTEST:LASER:"
    vals = {s: ca.get(P + s) for s in ("ShutterOpen_RBV", "OutputEnabled", "State", "Attenuator_RBV",
                                         "PulseDuration_RBV", "PPDivider_RBV", "PPEnable_RBV",
                                         "BurstCount_RBV", "OutputPower", "OutputEnergy", "Errors", "Warnings")}
    check("Laser readbacks", all(v is not None for v in vals.values()), str(vals))
    check("Laser setpoints seeded", abs(ca.get(P + "Attenuator") - vals["Attenuator_RBV"]) < 0.5)
    n, _ = ca.count_updates(P + "Attenuator_RBV", 4)
    check("Laser updates (0.5 s scan)", 6 <= n <= 10, f"{n} updates/4s")
    try:
        ca.put(P + "Attenuator", float(vals["Attenuator_RBV"]))
        check("Laser --read-only blocks writes", False, "write accepted!")
    except Exception as e:
        check("Laser --read-only blocks writes", True, type(e).__name__)


def keithley_checks(ca):
    P = "HWTEST:K:"
    check("Keithley Idn", "6221" in ca.get(P + "Idn"), ca.get(P + "Idn"))
    check("Keithley 2182A present", ca.get(P + "NanovoltmeterPresent") == "On")
    check("Keithley idle", ca.get(P + "State") == "Idle" and ca.get(P + "Busy") == "Off")
    n, _ = ca.count_updates(P + "PointsDone", 3)
    check("Keithley silent while idle", n <= 1, f"{n} updates/3s (1 = initial value)")


# ---------------------------------------------------------------- main
def discovery_checks(devices, connected):
    """Directory IOC -> fastq.discovery -> ophyd devices -> read() (reads only)."""
    sys.path.insert(0, str(ROOT / "control"))
    os.environ["FASTQ_DIRECTORY"] = "HWTEST:DIR:"
    try:
        from fastq.discovery import device_name, discover, read_directory

        entries = read_directory()
        check("directory lists every device", len(entries) == len(devices), str([e["prefix"] for e in entries]))
        found = discover(timeout=10)
        expected = {device_name(p) for n, (p, _, _) in devices.items() if connected.get(n)}
        check("discovery built every connected device", expected <= set(found), f"found {sorted(found)}")
        for name, dev in sorted(found.items()):
            data = dev.read()
            hinted = {k: round(v["value"], 6) if isinstance(v["value"], float) else v["value"]
                      for k, v in data.items() if k in dev.hints.get("fields", [])}
            check(f"ophyd {name}.read()", len(data) > 0, f"{len(data)} signals, hinted {hinted}")
    except Exception as e:
        check("discovery", False, f"{type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--itc", default="GPIB0::24::INSTR")
    ap.add_argument("--keithley", default="GPIB0::18::INSTR")
    ap.add_argument("--mfli-host", default="192.168.1.94")
    ap.add_argument("--mfli-device", default="dev1234")
    ap.add_argument("--demod", type=int, default=1)
    ap.add_argument("--laser-host", default="192.168.1.10")
    ap.add_argument("--skip", default="", help="comma list of itc,keithley,mfli,laser to leave out")
    ap.add_argument("--logdir", default=str(ROOT / "tests" / "hwtest_logs"))
    a = ap.parse_args()
    skip = {x.strip() for x in a.skip.split(",") if x.strip()}

    print("=== 1. snapshot BEFORE (direct queries)")
    before = snapshot(a)
    print(json.dumps(before, indent=1, default=str))

    py = sys.executable
    logdir = Path(a.logdir)
    logdir.mkdir(parents=True, exist_ok=True)
    for f in logdir.glob("*.log"):
        f.unlink()
    devices = {  # name: (prefix, command, checks)
        "itc": ("HWTEST:ITC:", ["ioc_host/iocs/itc503_ioc.py", "--gpib", a.itc], itc_checks),
        "keithley": ("HWTEST:K:", ["ioc_host/iocs/keithley_ioc.py", "--resource", a.keithley], keithley_checks),
        "mfli": ("HWTEST:MFLI:", ["ioc_host/iocs/mfli_ioc.py", "--device", a.mfli_device, "--labone-host", a.mfli_host,
                                  "--demod-index", str(a.demod)], mfli_checks),
        "laser": ("HWTEST:LASER:", ["ioc_host/iocs/carbide_ioc.py", "--host", a.laser_host, "--read-only"], laser_checks),
    }
    devices = {k: v for k, v in devices.items() if k not in skip}
    procs = {}
    print(f"\n=== 2. starting {len(devices)} IOCs on this host: {', '.join(devices)}")
    for name, (prefix, cmd, _) in devices.items():
        log = open(logdir / f"{name}.log", "w")
        procs[name] = (subprocess.Popen([py, "-u", *cmd, "--prefix", prefix], cwd=ROOT,
                                        stdout=log, stderr=subprocess.STDOUT), log)
        time.sleep(1.0)
    # directory IOC announcing the HWTEST devices (same mechanism as FASTQ:DIR: in the lab)
    import yaml
    dir_cfg = logdir / "hwtest_devices.yaml"
    dir_cfg.write_text(yaml.safe_dump({"devices": {
        f"{n}_ioc": {"ioc_script": Path(cmd[0]).stem, "enabled": True, "args": f"--prefix {prefix}"}
        for n, (prefix, cmd, _) in devices.items()}}))
    log = open(logdir / "directory.log", "w")
    procs["directory"] = (subprocess.Popen([py, "-u", "ioc_host/iocs/directory_ioc.py", "--config", str(dir_cfg),
                                            "--prefix", "HWTEST:DIR:"], cwd=ROOT, stdout=log,
                                           stderr=subprocess.STDOUT), log)

    ca = CA()
    try:
        print("\n=== 3. Channel Access checks")
        connected = {n: check(f"{n} IOC Connected", wait_connected(ca, p)) for n, (p, _, _) in devices.items()}
        for name, (prefix, _, fn) in devices.items():
            if not connected[name]:
                continue
            try:
                fn(ca)
            except Exception as e:
                check(f"{name} checks", False, f"aborted: {type(e).__name__}: {e}")
        if len(devices) > 1:
            check("all IOCs reachable at once (ca_windows)", all(connected.values()) and all(
                ca.get(p + "Connected") in ("On", 1) for p, _, _ in devices.values()))
        discovery_checks(devices, connected)
    finally:
        print("\n=== 4. stopping IOCs")
        try:
            ca.ctx.disconnect()  # close CA circuits first: avoids caproto noise when IOCs exit
        except Exception:
            pass
        for name, (p, log) in procs.items():
            p.terminate()
            p.wait(10)
            log.close()

    time.sleep(2)
    print("\n=== 5. snapshot AFTER + diff")
    after = snapshot(a)
    for dev in before:
        same = before[dev] == after[dev]
        check(f"{dev} state unchanged", same, "" if same else f"before={before[dev]} after={after[dev]}")

    for f in sorted(logdir.glob("*.log")):
        text = f.read_text(errors="replace")
        if f.name == "laser.log":
            # the one expected error: the deliberate write refused by --read-only
            check("laser write refused by read-only guard", "read-only: refused" in text)
            text = text.split("Invalid write request")[0].rsplit("\n", 1)[0]
        bad = [l.strip() for l in text.splitlines() if any(w in l for w in ("Traceback", "ERROR", "FAILED"))]
        check(f"log {f.name} clean", not bad, "; ".join(bad[:3]))

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n=== {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
