"""Headless IOC supervisor: the GUI's process manager (process_manager.py)
without Tk, for running an IOC host as a Windows service / systemd unit.

    python ioc_host/supervisor/supervisor.py --headless --config ioc_host/supervisor_config.yaml
    python ioc_host/supervisor/headless.py --config ioc_host/supervisor_config.yaml --log-dir D:/fastq-logs

Starts every enabled device of the host file, restarts the ones that exit with
the same back-off as the GUI, and writes supervisor.log plus one rotating log per
device to --log-dir (default ioc_host/logs/<host file name>). SIGINT, SIGTERM and
CTRL_BREAK (also what NSSM and `systemctl stop` send) stop every IOC, then exit 0.

The host file is only read, never written: edit it (or use the GUI on a test
copy), then restart the service.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import queue
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

TICK_S = 1.0
LOG_BYTES, LOG_BACKUPS = 5_000_000, 5

log = logging.getLogger("supervisor")


def kill_children_with_me() -> bool:
    """Windows: put this process in a job object that kills every process in it
    when its last handle closes. The IOCs inherit the job, so a hard stop of the
    supervisor (Task Scheduler "End", service stop, TerminateProcess) takes its
    IOCs with it instead of leaving them holding the instruments' ports. On
    Linux systemd's control group does the same."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = k32.CreateJobObjectW(None, None)
    info = ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = (job and k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))  # Extended...
          and k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()))
    if not ok:
        log.warning("could not create the kill-on-close job object (error %d) - IOCs survive a hard stop",
                    ctypes.get_last_error())
    # the handle is deliberately never closed: closing it is what kills the IOCs
    return bool(ok)


def _file_handler(path: Path, fmt: str) -> logging.Handler:
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter(fmt))
    return handler


class DeviceLogs:
    """One rotating file per device: an IOC's own output is what you need when it crash-loops."""

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.loggers: dict[str, logging.Logger] = {}

    def write(self, device_id: str, line: str) -> None:
        logger = self.loggers.get(device_id)
        if logger is None:
            logger = logging.getLogger(f"ioc.{device_id}")
            logger.propagate = False
            logger.setLevel(logging.INFO)
            logger.handlers[:] = [_file_handler(self.log_dir / f"{device_id}.log", "%(asctime)s %(message)s")]
            self.loggers[device_id] = logger
        logger.info(line)

    def close(self) -> None:
        for logger in self.loggers.values():
            for handler in logger.handlers:
                handler.close()


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(message)s"
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(_file_handler(log_dir / "supervisor.log", fmt))
    if sys.stderr is not None:  # none under pythonw
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(fmt))
        root.addHandler(console)


def run(config_path: Path, log_dir: Path) -> int:
    from config import load_config
    from process_manager import ProcessSupervisor
    import deps

    stop_requested: list[str] = []

    def request_stop(signum, _frame) -> None:
        stop_requested.append(signal.Signals(signum).name)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):  # SIGBREAK: Windows CTRL_BREAK / console close
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), request_stop)

    if not config_path.is_file():
        log.error("host file %s not found", config_path)
        return 2
    missing = deps.missing_packages()
    if missing:  # never pip-install from a service; the installer does that
        log.warning("missing packages %s - IOCs that need them will crash-loop", ", ".join(missing))

    config = load_config(config_path)
    output: queue.Queue[tuple[str, str]] = queue.Queue()
    sup = ProcessSupervisor(config, output_queue=output)
    device_logs = DeviceLogs(log_dir)

    def drain() -> None:
        while True:
            try:
                device_id, line = output.get_nowait()
            except queue.Empty:
                return
            device_logs.write(device_id, line)

    enabled = [d for d, m in sorted(sup.devices.items()) if m.config.enabled]
    log.info("supervisor pid %d, host file %s, logs %s, python %s", os.getpid(), config_path,
             log_dir, sup._python_exe())
    log.info("starting %d enabled device(s): %s", len(enabled), ", ".join(enabled) or "none")
    for device_id in enabled:
        sup.start(device_id)

    last_status: dict[str, tuple] = {}
    try:
        while not stop_requested:
            sup.tick()
            drain()
            for device_id, managed in sup.devices.items():
                state = (managed.status.split(" - ")[0], managed.pid)
                if last_status.get(device_id) != state:
                    last_status[device_id] = state
                    log.info("%s: %s%s", device_id, managed.status, f" (pid {managed.pid})" if managed.pid else "")
            time.sleep(TICK_S)
    except KeyboardInterrupt:  # a Ctrl-C that arrived before/outside the handler
        stop_requested.append("SIGINT")
    finally:
        log.info("stopping all IOCs (%s)", stop_requested[0] if stop_requested else "error")
        sup.shutdown_all()
        time.sleep(0.2)  # let the reader threads hand over the IOCs' last lines
        drain()
        device_logs.close()
        log.info("supervisor stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    from config import DEFAULT_CONFIG_PATH, IOC_HOST_DIR

    ap = argparse.ArgumentParser(description="Run the enabled IOCs of a host file without the GUI.")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                    help="host file (default: $FASTQ_IOC_CONFIG or ioc_host/supervisor_config.yaml)")
    ap.add_argument("--log-dir", type=Path, help="default: ioc_host/logs/<host file name>")
    a = ap.parse_args(argv)
    config_path = a.config.resolve()
    log_dir = (a.log_dir or IOC_HOST_DIR / "logs" / config_path.stem).resolve()
    setup_logging(log_dir)
    ignore = IOC_HOST_DIR / "logs" / ".gitignore"
    if not a.log_dir and not ignore.exists():  # rotated logs (x.log.1) would otherwise show up in git
        ignore.write_text("*\n", encoding="utf-8")
    kill_children_with_me()
    try:
        return run(config_path, log_dir)
    except Exception:  # into supervisor.log: a service has nobody reading its stderr
        log.exception("supervisor failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
