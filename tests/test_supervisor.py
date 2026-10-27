"""Headless IOC supervisor (ioc_host/supervisor/headless.py): starts a mock IOC
from a temporary host file, restarts it when it is killed, stops everything on
CTRL_BREAK / SIGTERM, and (Windows) takes its IOCs along when it is hard-killed.
Prefixes TESTSUP:*; only processes started here are touched.

    python -m pytest tests/test_supervisor.py -q
"""
from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest
from caproto.sync.client import read

ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "ioc_host" / "supervisor" / "supervisor.py"
PREFIX = "TESTSUP:FLOW:"
HOST_FILE = f"""\
devices:
  flow:
    ioc_script: voegtlin_ioc
    enabled: true
    args: '--mock --prefix {PREFIX}'
  spare:
    ioc_script: voegtlin_ioc
    enabled: false
    args: '--mock --prefix TESTSUP:SPARE:'
restart: {{backoff_initial_s: 1.0, backoff_max_s: 2.0, backoff_reset_after_s: 120.0}}
"""


def ioc_pid(timeout: float = 40.0, not_pid: int | None = None) -> int:
    """PID the IOC reports over Channel Access (IOC:PROCESS_ID), waiting until it answers."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            pid = int(read(PREFIX + "IOC:PROCESS_ID", timeout=2).data[0])
            if pid != not_pid:
                return pid
        except Exception:
            pass
        time.sleep(0.5)
    raise AssertionError(f"{PREFIX} did not answer over CA within {timeout} s")


class Supervisor:
    def __init__(self, tmp_path: Path) -> None:
        host = tmp_path / "testhost.yaml"
        host.write_text(HOST_FILE, encoding="utf-8")
        self.host_text = HOST_FILE
        self.host, self.log_dir = host, tmp_path / "logs"
        flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else \
            {"start_new_session": True}
        self.proc = subprocess.Popen([sys.executable, str(SUPERVISOR), "--headless", "--config", str(host),
                                      "--log-dir", str(self.log_dir)], cwd=ROOT, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, **flags)
        self.seen: dict[int, psutil.Process] = {}

    def descendants(self) -> list[psutil.Process]:
        """Remembers every process started under the supervisor (psutil.Process keeps the
        create time, so a reused PID is never mistaken for ours)."""
        try:
            for p in psutil.Process(self.proc.pid).children(recursive=True):
                self.seen.setdefault(p.pid, p)
        except psutil.NoSuchProcess:
            pass
        return list(self.seen.values())

    def alive(self) -> list[psutil.Process]:
        def running(p: psutil.Process) -> bool:
            try:
                return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                return False

        return [p for p in self.seen.values() if running(p)]

    def stop(self) -> int:
        if sys.platform == "win32":
            self.proc.send_signal(signal.CTRL_BREAK_EVENT)  # what NSSM / a console Ctrl-Break sends
        else:
            self.proc.send_signal(signal.SIGTERM)  # what systemd (KillMode=mixed) sends
        return self.proc.wait(60)

    def cleanup(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)
        for p in self.alive():
            p.kill()


@pytest.fixture
def supervisor(tmp_path):
    sup = Supervisor(tmp_path)
    yield sup
    sup.cleanup()


def wait_until_gone(sup: Supervisor, timeout: float = 15.0) -> list[psutil.Process]:
    deadline = time.monotonic() + timeout
    while sup.alive() and time.monotonic() < deadline:
        time.sleep(0.2)
    return sup.alive()


def test_start_restart_stop(supervisor):
    pid = ioc_pid()
    assert pid in [p.pid for p in supervisor.descendants()], "the IOC answering is not ours"

    psutil.Process(pid).kill()  # the IOC crashes ...
    new_pid = ioc_pid(not_pid=pid)  # ... and is started again with the back-off (1 s here)
    assert new_pid in [p.pid for p in supervisor.descendants()]

    assert supervisor.stop() == 0
    assert not wait_until_gone(supervisor), "IOC processes left running after the supervisor stopped"

    log = (supervisor.log_dir / "supervisor.log").read_text(encoding="utf-8")
    assert "starting 1 enabled device(s): flow" in log and "supervisor stopped" in log
    assert "exited with code" in (supervisor.log_dir / "flow.log").read_text(encoding="utf-8")
    assert not (supervisor.log_dir / "spare.log").exists()  # disabled devices are not started
    assert supervisor.host.read_text(encoding="utf-8") == supervisor.host_text  # host file never rewritten


@pytest.mark.skipif(sys.platform != "win32", reason="Linux: systemd's control group kills the IOCs")
def test_hard_kill_takes_iocs_along(supervisor):
    """Task Scheduler "End" / a service stop is TerminateProcess - no signal to handle."""
    ioc_pid()
    # a venv's python.exe is a launcher that runs the real interpreter as a child (in its own
    # kill-on-close job); kill the interpreter itself, so only headless.py's job can save us
    inner = [p for p in supervisor.descendants() if "supervisor.py" in " ".join(p.cmdline())]
    (inner[0] if inner else psutil.Process(supervisor.proc.pid)).kill()
    supervisor.proc.wait(10)
    assert not wait_until_gone(supervisor), "IOCs survived a hard kill of the supervisor"
