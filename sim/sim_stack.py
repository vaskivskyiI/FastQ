"""Simulated IOC host: the real IOCs with simulated instruments, under TEST:*
prefixes (no clash with the lab), plus the directory IOC TEST:DIR:.

    python sim/sim_stack.py                     # Ctrl-C stops everything
    python sim/sim_stack.py --devices sim/sim_host2.yaml --directory TEST:DIR:H2: --peers TEST:DIR: --no-carbide
                                                # a second simulated IOC host (decentralised lab)

Then point clients at it with FASTQ_DIRECTORY=TEST:DIR: (see README).
"""
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEVICES = Path(__file__).with_name("sim_devices.yaml")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logdir", default=str(ROOT / "sim" / "logs"))
    ap.add_argument("--devices", default=str(DEVICES), help="device list (supervisor_config format)")
    ap.add_argument("--directory", default="TEST:DIR:", help="prefix of this host's directory IOC")
    ap.add_argument("--peers", default="", help="other hosts' directory prefixes, comma-separated")
    ap.add_argument("--no-carbide", action="store_true", help="no mock Carbide REST server")
    a = ap.parse_args()
    devices = Path(a.devices)
    py, logdir = sys.executable, Path(a.logdir)
    logdir.mkdir(exist_ok=True)
    cmds = {} if a.no_carbide else {"carbide_rest": [py, "-u", "sim/mock_carbide_server.py", "--port", "20099"]}
    cmds["directory"] = [py, "-u", "ioc_host/iocs/directory_ioc.py", "--config", str(devices), "--prefix", a.directory,
                         *(["--peers", a.peers] if a.peers else [])]
    for name, dev in yaml.safe_load(devices.read_text())["devices"].items():
        args = shlex.split(dev["args"])
        cmds[name] = [py, "-u", f"ioc_host/iocs/{dev['ioc_script']}.py", *args]
    procs = []
    try:
        for name, cmd in cmds.items():
            log = open(logdir / f"{a.directory.replace(':', '_')}{name}.log", "w")
            procs.append((name, subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT), log))
            time.sleep(1.0 if name == "carbide_rest" else 0.3)
        print(f"simulated IOC host running ({len(procs)} processes, logs in {logdir}) - Ctrl-C to stop", flush=True)
        while True:
            for name, p, _ in procs:
                if p.poll() is not None:
                    raise SystemExit(f"{name} exited ({p.returncode}) - see {logdir}")
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for _, p, log in procs:
            p.terminate()
        for _, p, log in procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
            log.close()


if __name__ == "__main__":
    main()
