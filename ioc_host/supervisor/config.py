"""Persistent supervisor config: which device INSTANCES exist, which
iocs/*_ioc.py TYPE each one runs, whether each is enabled, and what CLI args
to launch it with. Kept separate from supervisor.py (no Tkinter import
here) so it's testable without a display.

A "type" is an iocs/*_ioc.py file (itc503_ioc, mfli_ioc, ...). An
"instance" is one entry in `devices` below - you can have more than one
instance of the same type (two ITC503s, say), each with its own PV prefix
and its own args, distinguished by `device_id` (the dict key / GUI row
name) while `ioc_script` says which type it runs.

File: ioc_host/supervisor_config.yaml - this PC's own (not in git); examples: ioc_host/hosts/*.example.yaml

    devices:
      itc503_ioc:                # device_id - also this type's PRIMARY instance,
        ioc_script: itc503_ioc
        enabled: true
        args: "--gpib GPIB0::23::INSTR --prefix FASTQ:ITC1:"
      itc503_ioc_2:               # a second ITC503 - same type, different instance
        ioc_script: itc503_ioc
        enabled: false
        args: ""
    known_types: [itc503_ioc, mfli_ioc, keithley_ioc, carbide_ioc]
    restart:
      backoff_initial_s: 2.0
      backoff_max_s: 60.0
      backoff_reset_after_s: 120.0
    python_executable: null    # null = same interpreter running the GUI

Nothing is ever added, enabled or rewritten automatically: a new iocs/*_ioc.py type
is offered in "Add device..." (with the args you type), and the file is written only
when you change something in the GUI - so comments in the host files survive.
(An earlier version auto-created an enabled instance of every new type with
transport PC's addresses on every host, and re-saved the file every 10 s.)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
IOC_HOST_DIR = REPO_ROOT / "ioc_host"
IOCS_DIR = IOC_HOST_DIR / "iocs"
# this PC's host file (git-ignored, made from ioc_host/hosts/*.example.yaml by start_ioc_host.bat);
# another one: FASTQ_IOC_CONFIG=<file> (start_ioc_host.bat <file>)
DEFAULT_CONFIG_PATH = Path(os.environ.get("FASTQ_IOC_CONFIG") or IOC_HOST_DIR / "supervisor_config.yaml").resolve()

DEFAULT_RESTART = {
    "backoff_initial_s": 2.0,
    "backoff_max_s": 60.0,
    "backoff_reset_after_s": 120.0,
}

@dataclass
class DeviceConfig:
    device_id: str
    ioc_script: str
    enabled: bool = True
    args: str = ""


@dataclass
class SupervisorConfig:
    devices: dict[str, DeviceConfig] = field(default_factory=dict)
    known_types: set[str] = field(default_factory=set)
    restart: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_RESTART))
    python_executable: str | None = None
    path: Path = DEFAULT_CONFIG_PATH


def discover_ioc_scripts(iocs_dir: Path = IOCS_DIR) -> list[str]:
    """Every iocs/*_ioc.py file, as type ids (filename without .py),
    sorted for a stable display order."""
    if not iocs_dir.is_dir():
        return []
    return sorted(p.stem for p in iocs_dir.glob("*_ioc.py"))


def unique_device_id(preferred: str, existing: dict[str, DeviceConfig]) -> str:
    """`preferred` if free, else preferred_2, preferred_3, ... - used both
    for auto-creating a type's primary instance and for the GUI's "Add
    device" dialog's suggested name."""
    if preferred not in existing:
        return preferred
    n = 2
    while f"{preferred}_{n}" in existing:
        n += 1
    return f"{preferred}_{n}"


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> SupervisorConfig:
    import yaml

    data: dict[str, Any] = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}

    devices = {
        device_id: DeviceConfig(
            device_id=device_id,
            # Back-compat with configs written before ioc_script existed:
            # those always had device_id == the type's script stem, so
            # that's the right fallback when the key is simply absent.
            ioc_script=str(entry["ioc_script"]),  # required: device ids are device names, not types
            enabled=bool(entry.get("enabled", True)),
            args=str(entry.get("args", "")),
        )
        for device_id, entry in (data.get("devices") or {}).items()
    }
    known_types = set(data.get("known_types") or [])
    restart = dict(DEFAULT_RESTART)
    restart.update(data.get("restart") or {})

    config = SupervisorConfig(
        devices=devices,
        known_types=known_types,
        restart=restart,
        python_executable=data.get("python_executable"),
        path=path,
    )
    reconcile_with_discovered_scripts(config)
    return config


def reconcile_with_discovered_scripts(config: SupervisorConfig, iocs_dir: Path = IOCS_DIR) -> bool:
    """Remember which IOC types exist (offered in "Add device..."); never adds, enables or
    changes an instance. Returns False: nothing for the caller to save."""
    config.known_types |= set(discover_ioc_scripts(iocs_dir))
    return False


def save_config(config: SupervisorConfig) -> None:
    import yaml

    data = {
        "devices": {
            device_id: {"ioc_script": dc.ioc_script, "enabled": dc.enabled, "args": dc.args}
            for device_id, dc in sorted(config.devices.items())
        },
        "known_types": sorted(config.known_types),
        "restart": config.restart,
        "python_executable": config.python_executable,
    }
    config.path.parent.mkdir(parents=True, exist_ok=True)
    with open(config.path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False)
