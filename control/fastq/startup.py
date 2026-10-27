"""Startup of the Bluesky environment - loaded by the queue server
(`start-re-manager --startup-module fastq.startup`) and usable interactively:

    cd control && python -i -m fastq.startup
    >>> RE(rt_sweep(detectors=["@lockin"], start_k=10, final_k=8, time_min=5, current_a=1e-6))

The plans are the functions in control/experiments/*.py (fastq.plan_registry);
they, the discovered devices and RE are what the queue server offers to clients.
"""
import logging

from bluesky import plan_stubs as bps  # noqa: F401

from fastq.plan_registry import load_plans  # noqa: E402
from fastq.session import connect, load_lab, make_run_engine, role  # noqa: F401

logging.getLogger("caproto").setLevel(logging.WARNING)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

lab = load_lab()
devices = connect(lab)
globals().update(devices)          # itc1, mfli1, laser1, ... as plain names
globals().update(load_plans(lab))  # every plan in control/experiments/*.py
RE = make_run_engine(lab)
from fastq.suspenders import install as _install_suspenders  # noqa: E402

_suspenders = _install_suspenders(RE, lab)  # lab.yaml suspenders: pause while e.g. the interlock is tripped
print(f"FastQ session ({lab['_path']}): devices {sorted(devices)}; suspenders: {[s._why for s in _suspenders]}")
