"""Build ophyd devices from the directory IOC - no prefixes or IPs in client code.

    from fastq.discovery import discover
    devices = discover()            # {"itc1": <Itc503Device>, "mfli1": <MfliDevice>, ...}

The directory PV (default FASTQ:DIR:Devices, override with the
FASTQ_DIRECTORY env var, e.g. TEST:DIR: for the simulator) is found by
normal CA name search. Each listed + enabled device is checked
(<prefix>IOC:TYPE must match) and created with the ophyd class built from
its schema (fastq.generic); unreachable devices are skipped with a warning, so a partly powered
lab still works. Device names come from the PV prefix: FASTQ:ITC2: -> itc2.
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor

import ophyd

ophyd.set_cl("caproto")  # pure-Python Channel Access, no EPICS base needed

from ophyd import EpicsSignalRO  # noqa: E402

from fastq.generic import device_class, read_schema  # noqa: E402

log = logging.getLogger("fastq.discovery")


def _quiet_exit() -> None:
    # at interpreter exit caproto's subscription threads still receive updates after the thread
    # pool is gone and log "cannot schedule new futures after shutdown" tracebacks - harmless noise
    logging.getLogger("caproto").setLevel(logging.CRITICAL)


try:  # threading's own exit hooks run before the thread pools shut down (atexit would be too late)
    import threading

    threading._register_atexit(_quiet_exit)
except (AttributeError, RuntimeError):
    pass


def directory_prefixes() -> list[str]:
    """FASTQ_DIRECTORY: one directory prefix, or several comma-separated
    (any reachable one is enough - peered directories share AllDevices)."""
    return [p.strip() for p in os.environ.get("FASTQ_DIRECTORY", "FASTQ:DIR:").split(",") if p.strip()]


def directory_prefix() -> str:
    return ",".join(directory_prefixes())


def device_name(prefix: str) -> str:
    return prefix.rstrip(":").split(":")[-1].lower()


def read_directory(prefix: str | None = None, timeout: float = 5.0) -> list[dict]:
    """Merged device list of all given directories (AllDevices, or Devices
    for a directory without peers support); duplicates by prefix removed."""
    merged, errors = {}, []
    for p in ([prefix] if prefix else directory_prefixes()):
        for pv in ("AllDevices", "Devices"):
            sig = EpicsSignalRO(p + pv, string=True, name="directory")
            try:
                sig.wait_for_connection(timeout=timeout)
                for d in json.loads(sig.get()):
                    merged.setdefault(d["prefix"], d)
                break
            except Exception:  # noqa: BLE001
                errors.append(p)
    if not merged and errors:
        raise ConnectionError(f"device directory {', '.join(dict.fromkeys(errors))} not reachable (IOC host not running?)")
    dp = os.environ.get("FASTQ_DERIVED_PREFIX")  # derived values IOC on the control host (fastq.derived)
    if dp and dp not in merged:
        merged[dp] = {"prefix": dp, "type": "derived", "enabled": True, "host": "control host"}
    return list(merged.values())


def discover(prefix: str | None = None, timeout: float = 5.0, include_disabled: bool = False) -> dict:
    entries = [e for e in read_directory(prefix, timeout) if e["enabled"] or include_disabled]

    def build(entry):  # every IOC describes itself (fastq.generic); all devices are tried at once
        name = device_name(entry["prefix"])
        try:
            cls = device_class(read_schema(entry["prefix"], timeout))
        except Exception:  # noqa: BLE001
            return name, None, "not running"
        dev = cls(entry["prefix"], name=name)
        try:
            dev.wait_for_connection(timeout=timeout)
            actual = dev.ioc.ioc_type.get()
        except Exception as exc:  # noqa: BLE001
            return name, None, f"not reachable ({type(exc).__name__})"
        if actual != entry["type"]:
            return name, None, f"IOC reports type {actual!r}, directory says {entry['type']!r}"
        return name, dev, None

    devices, missing = {}, {}
    with ThreadPoolExecutor(max(1, min(16, len(entries)))) as pool:
        for name, dev, why in pool.map(build, entries):
            if dev is not None:
                devices[name] = dev
            else:
                missing.setdefault(why, []).append(name)
    for why, names in missing.items():  # one line per reason, not one per device
        log.warning("%d device(s) skipped, %s: %s", len(names), why, ", ".join(sorted(names)))
    return devices


def main() -> None:
    """python -m fastq.discovery : list what the directory announces and what is reachable."""
    logging.basicConfig(level=logging.INFO)
    entries = read_directory()
    found = discover(include_disabled=True)
    print(f"directory {directory_prefix()}Devices:")
    for e in entries:
        name = device_name(e["prefix"])
        state = "ONLINE" if name in found else ("disabled" if not e["enabled"] else "unreachable")
        print(f"  {name:12s} {e['type']:10s} {e['prefix']:20s} {e.get('host', ''):16s} {state}")


if __name__ == "__main__":
    main()
