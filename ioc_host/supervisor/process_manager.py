"""Subprocess lifecycle for the IOC supervisor: start/stop, crash detection,
and restart with backoff. No Tkinter here - the GUI polls `tick()` from its
own event loop and drains `output_queue` to update widgets, so this class
is usable/testable headlessly.

One thing this deliberately does NOT do: guess whether a newly-added device
with empty/missing args is "supposed" to fail. If a device is enabled but
misconfigured (missing --gpib, wrong host, ...), it starts, exits almost
immediately with an argparse or connection error, and this manager treats
that exactly like any other crash - logs it, backs off, retries. That is
how a genuinely new device ends up "running" once someone fills in its
real address: it was already being retried the whole time.
"""
from __future__ import annotations

import logging
import queue
import shlex
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from config import (
    DeviceConfig,
    SupervisorConfig,
    IOCS_DIR,
    reconcile_with_discovered_scripts,
    save_config,
    unique_device_id,
)

logger = logging.getLogger("process_manager")

LOG_LINES_KEPT = 4000


@dataclass
class ManagedDevice:
    device_id: str
    config: DeviceConfig
    script_path: Path
    process: subprocess.Popen | None = None
    status: str = "stopped"  # stopped | running | crashed | disabled | missing | starting
    pid: int | None = None
    restart_count: int = 0
    next_restart_at: float | None = None  # time.monotonic() deadline
    started_at: float | None = None
    intentional_stop: bool = True  # True until start() is called
    log: deque = field(default_factory=lambda: deque(maxlen=LOG_LINES_KEPT))

    @property
    def script_exists(self) -> bool:
        return self.script_path.is_file()


class ProcessSupervisor:
    def __init__(
        self,
        config: SupervisorConfig,
        iocs_dir: Path = IOCS_DIR,
        output_queue: "queue.Queue[tuple[str, str]]" | None = None,
    ) -> None:
        self.config = config
        self.iocs_dir = iocs_dir
        self.output_queue = output_queue if output_queue is not None else queue.Queue()
        self.devices: dict[str, ManagedDevice] = {}
        self.sync_with_config()

    # ------------------------------------------------------------------
    # Discovery / config sync
    # ------------------------------------------------------------------

    def sync_with_config(self) -> list[str]:
        """Rebuild self.devices from self.config.devices plus whatever
        scripts are actually on disk right now - reconcile_with_discovered_scripts
        (see config.py) is what decides whether a type with zero instances
        gets a new primary instance auto-created, so this just applies that
        decision to self.devices and reports which device_ids are new since
        the last sync (for the GUI to announce)."""
        before = set(self.config.devices)
        changed = reconcile_with_discovered_scripts(self.config, self.iocs_dir)
        new_ids = sorted(set(self.config.devices) - before)

        # A device_id whose config entry was removed elsewhere (see remove())
        # won't be in self.config.devices any more - drop its ManagedDevice too.
        for stale_id in set(self.devices) - set(self.config.devices):
            del self.devices[stale_id]

        for device_id, dc in self.config.devices.items():
            script_path = self.iocs_dir / f"{dc.ioc_script}.py"
            if device_id in self.devices:
                managed = self.devices[device_id]
                managed.config = dc
                managed.script_path = script_path
            else:
                managed = ManagedDevice(device_id=device_id, config=dc, script_path=script_path)
                self.devices[device_id] = managed

            if not managed.script_exists:
                managed.status = "missing"
            elif not dc.enabled and managed.process is None:
                managed.status = "disabled"

        if changed:
            save_config(self.config)
        return new_ids

    def add_device(self, ioc_script: str, device_id: str | None = None, args: str = "") -> str:
        """Adds a new device instance of type `ioc_script`. If `device_id`
        is omitted, suggests one (the type name if free, else type_2,
        type_3, ...). Starts disabled with blank/given args on purpose -
        there is no address to guess. Returns the device_id used."""
        device_id = unique_device_id(device_id or ioc_script, self.config.devices)
        self.config.devices[device_id] = DeviceConfig(
            device_id=device_id, ioc_script=ioc_script, enabled=False, args=args
        )
        self.config.known_types.add(ioc_script)
        save_config(self.config)
        self.sync_with_config()
        return device_id

    def remove(self, device_id: str) -> None:
        """Stops the device if running and deletes its config entry -
        known_types keeps its type from being auto-resurrected on the next
        rescan (see reconcile_with_discovered_scripts)."""
        if device_id not in self.devices:
            return
        self.stop(device_id)
        self.config.devices.pop(device_id, None)
        self.devices.pop(device_id, None)
        save_config(self.config)

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def _python_exe(self) -> str:
        return self.config.python_executable or sys.executable

    def start(self, device_id: str) -> None:
        managed = self.devices[device_id]
        if managed.process is not None and managed.process.poll() is None:
            return  # already running
        if not managed.script_exists:
            managed.status = "missing"
            return

        args = shlex.split(managed.config.args) if managed.config.args.strip() else []
        cmd = [self._python_exe(), "-u", str(managed.script_path), *args]
        logger.info("Starting %s: %s", device_id, " ".join(cmd))

        try:
            process = subprocess.Popen(
                cmd,
                cwd=str(self.iocs_dir.parents[1]),  # repo root
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            managed.log.append(f"[supervisor] failed to launch: {exc}")
            managed.status = "crashed"
            self._schedule_restart(managed)
            return

        managed.process = process
        managed.pid = process.pid
        managed.status = "starting"
        managed.started_at = time.monotonic()
        managed.intentional_stop = False
        managed.next_restart_at = None

        thread = threading.Thread(target=self._reader_thread, args=(managed, process), daemon=True)
        thread.start()

    def _reader_thread(self, managed: ManagedDevice, process: subprocess.Popen) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip("\n")
            managed.log.append(line)
            self.output_queue.put((managed.device_id, line))
        # stdout closed -> process has exited (or is exiting)

    def stop(self, device_id: str) -> None:
        managed = self.devices[device_id]
        managed.intentional_stop = True
        managed.next_restart_at = None
        process = managed.process
        if process is None or process.poll() is not None:
            managed.status = "disabled" if not managed.config.enabled else "stopped"
            return

        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        except Exception:
            logger.exception("Error stopping %s", device_id)
        managed.status = "disabled" if not managed.config.enabled else "stopped"

    def restart(self, device_id: str) -> None:
        self.stop(device_id)
        self.start(device_id)

    def shutdown_all(self) -> None:
        for device_id, managed in self.devices.items():
            if managed.process is not None and managed.process.poll() is None:
                self.stop(device_id)

    # ------------------------------------------------------------------
    # Config-driven actions
    # ------------------------------------------------------------------

    def set_enabled(self, device_id: str, enabled: bool) -> None:
        managed = self.devices[device_id]
        managed.config.enabled = enabled
        save_config(self.config)
        if enabled:
            managed.restart_count = 0
            self.start(device_id)
        else:
            self.stop(device_id)

    def set_args(self, device_id: str, args: str) -> None:
        managed = self.devices[device_id]
        managed.config.args = args
        save_config(self.config)
        was_running = managed.process is not None and managed.process.poll() is None
        if was_running:
            self.restart(device_id)

    # ------------------------------------------------------------------
    # Crash detection + backoff - call this every ~1s from the GUI loop
    # ------------------------------------------------------------------

    def tick(self) -> None:
        now = time.monotonic()
        for device_id, managed in self.devices.items():
            process = managed.process
            if process is not None:
                returncode = process.poll()
                if returncode is not None:
                    # it just exited
                    managed.process = None
                    managed.pid = None
                    if managed.intentional_stop:
                        managed.status = "disabled" if not managed.config.enabled else "stopped"
                    else:
                        managed.log.append(f"[supervisor] exited with code {returncode}")
                        self.output_queue.put((device_id, f"[supervisor] exited with code {returncode}"))
                        self._schedule_restart(managed)
                elif managed.status == "starting":
                    # ran for a bit without exiting - consider it up
                    managed.status = "running"

            if (
                managed.next_restart_at is not None
                and now >= managed.next_restart_at
                and managed.config.enabled
                and not managed.intentional_stop
            ):
                self.start(device_id)

    def _schedule_restart(self, managed: ManagedDevice) -> None:
        restart_cfg = self.config.restart
        # reset the backoff if it had been running healthily for a while
        if managed.started_at is not None:
            uptime = time.monotonic() - managed.started_at
            if uptime >= restart_cfg["backoff_reset_after_s"]:
                managed.restart_count = 0

        delay = min(
            restart_cfg["backoff_initial_s"] * (2 ** managed.restart_count),
            restart_cfg["backoff_max_s"],
        )
        managed.restart_count += 1
        managed.next_restart_at = time.monotonic() + delay
        managed.status = f"crashed - restarting in {delay:.0f}s"
