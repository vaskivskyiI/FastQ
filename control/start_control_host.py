"""Start the CONTROL HOST services (normally on the IOC host itself):

    Redis        127.0.0.1:6379   queue persistence for the queue server (fakeredis,
                                  pure Python - Redis has no Windows build; lab.yaml queue.redis_port)
    Tiled        0.0.0.0:8000     data server: every run, readable from any PC
    RE Manager   tcp://*:60615    Bluesky queue server (control), tcp://*:60625 (info)
    Dashboards   0.0.0.0:8088     web GUI for everything (control/web/server.py)
    ELOG service                  posts run/comment/device entries (only if lab.yaml elog.url is set)
    MCP server   127.0.0.1:8089   AI assistants (only if lab.yaml ai.mcp_port is set; fastq/mcp_server.py)

    python control/start_control_host.py                   # lab (control/lab.yaml)
    python control/start_control_host.py --lab control/lab_sim.yaml

Needs the IOCs to be running (devices are discovered at environment start).
Clients then connect with the qserver CLI / bluesky-queueserver-api and the
tiled client (see README). Tiled API key: env FASTQ_TILED_API_KEY (a key is
generated and stored in <data>/tiled/api_key.txt on first start if unset).
"""
from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path


CONTROL = Path(__file__).resolve().parent
sys.path.insert(0, str(CONTROL))
from fastq.session import load_lab, tiled_dir  # noqa: E402
QS = CONTROL / "qserver"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lab", default=str(CONTROL / "lab.yaml"))
    ap.add_argument("--data-dir", help="Tiled catalog + files (default: next to the JSONL backup dir)")
    ap.add_argument("--tiled-port", type=int, help="default: the port of lab.yaml data.tiled_uri (8000)")
    ap.add_argument("--web-port", type=int, default=8088)
    ap.add_argument("--no-open", action="store_true", help="do not open the RE environment automatically")
    a = ap.parse_args()
    lab_path = Path(a.lab).resolve()
    lab = load_lab(lab_path)
    if a.tiled_port is None:
        from urllib.parse import urlparse

        a.tiled_port = urlparse((lab.get("data") or {}).get("tiled_uri") or "http://127.0.0.1:8000").port or 8000
    data_dir = Path(a.data_dir) if a.data_dir else tiled_dir(lab)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:  # typically a lab file copied from another PC
        raise SystemExit(f"Cannot create the data folder {data_dir} ({e.strerror}).\n"
                         f"Set data.backup_dir in {lab_path} to a folder of this PC.")
    key_file = data_dir / "api_key.txt"
    api_key = os.environ.get("FASTQ_TILED_API_KEY") or (key_file.read_text().strip() if key_file.exists() else None)
    if not api_key:
        api_key = secrets.token_hex(16)
        key_file.write_text(api_key)
    env = dict(os.environ, PYTHONPATH=str(CONTROL), FASTQ_LAB=str(lab_path), FASTQ_TILED_API_KEY=api_key,
               FASTQ_TILED_DIR=str(data_dir))
    scripts = Path(sys.executable).parent
    d = data_dir.as_posix()
    procs = []
    # helpers restarted when they exit (also to reload their code; the derived-values IOC exits on a new definition)
    RESTARTABLE = {"web", "elog", "derived", "backup"}

    def start(name, cmd):
        log = open(data_dir / f"{name}.log", "a" if name in RESTARTABLE else "w")
        procs.append([name, subprocess.Popen(cmd, env=env, cwd=CONTROL, stdout=log, stderr=subprocess.STDOUT), log, cmd])

    try:
        redis_port = int((lab.get("queue") or {}).get("redis_port", 6379))  # lab.yaml queue.redis_port
        start("redis", [sys.executable, "-c", "from fakeredis import TcpFakeServer as S; "
                        f"S(('127.0.0.1', {redis_port}), server_type='redis').serve_forever()"])
        # catalog init via the wrapper (tiled's own --init spawns a plain `python -m tiled`, see tiled_server.py)
        subprocess.run([sys.executable, str(CONTROL / "tiled_server.py"), "catalog", "init", "--if-not-exists",
                        f"sqlite+aiosqlite:///{d}/catalog.db"], env=env, cwd=CONTROL, check=True)
        # camera frames stay in the HDF5 files the detector IOCs wrote; Tiled may read them from
        # these folders (data.readable_storage: local paths or shares of the detector PCs)
        readable = []
        for r in (lab.get("data") or {}).get("readable_storage") or []:
            r = Path(r) if Path(r).is_absolute() or str(r).startswith("\\\\") else CONTROL.parent / r
            readable += ["-r", str(r)]
        start("tiled", [sys.executable, str(CONTROL / "tiled_server.py"), "serve", "catalog", f"{d}/catalog.db",
                        "-w", f"{d}/files", "-w", f"sqlite:///{d}/tables.sqlite", *readable, "--host", "0.0.0.0",
                        "--port", str(a.tiled_port), "--api-key", api_key])
        time.sleep(3)
        # the queue server only allows plans/devices listed here - regenerate from the live environment
        subprocess.run([str(scripts / "qserver-list-plans-devices"), "--startup-module", "fastq.startup",
                        "--file-name", str(QS / "existing_plans_and_devices.yaml")], env=env, cwd=CONTROL, check=True)
        # --startup-module (not --startup-dir): the queue server's dir mode breaks on Windows paths
        start("re_manager", [str(scripts / "start-re-manager"), "--startup-module", "fastq.startup",
                             "--existing-plans-devices", str(QS / "existing_plans_and_devices.yaml"),
                             "--user-group-permissions", str(QS / "user_group_permissions.yaml"),
                             "--redis-addr", f"127.0.0.1:{redis_port}", "--zmq-publish-console", "ON",
                             # re-read control/experiments/*.py whenever the environment is (re)opened
                             "--update-existing-plans-devices", "ENVIRONMENT_OPEN"])
        start("web", [sys.executable, str(CONTROL / "web" / "server.py"), "--lab", str(lab_path),
                      "--port", str(a.web_port)])
        dp = (lab.get("derived") or {}).get("prefix")
        if dp:  # live derived values (fastq.derived), needs the device directory
            env["FASTQ_DERIVED_PREFIX"] = dp
            if lab.get("directory"):
                env.setdefault("FASTQ_DIRECTORY", lab["directory"])
            start("derived", [sys.executable, str(CONTROL.parent / "ioc_host" / "iocs" / "derived_ioc.py"),
                              "--lab", str(lab_path), "--prefix", dp])
        if (lab.get("backup") or {}).get("target"):  # copies of the data folder (fastq.backup)
            start("backup", [sys.executable, "-m", "fastq.backup", "--loop", "--lab", str(lab_path)])
        if (lab.get("elog") or {}).get("url"):
            start("elog", [sys.executable, "-m", "fastq.elog"])
        ai = lab.get("ai") or {}
        if ai.get("mcp_port"):  # AI assistants over the network (stdio clients start their own)
            env["FASTQ_URL"] = f"http://127.0.0.1:{a.web_port}"
            env["FASTQ_AI_ACCESS"] = ai.get("access", "read")
            start("mcp", [sys.executable, "-m", "fastq.mcp_server", "--http", "--host", ai.get("host", "127.0.0.1"),
                          "--port", str(ai["mcp_port"])])
        if not a.no_open:
            from bluesky_queueserver_api.zmq import REManagerAPI

            api = REManagerAPI()
            time.sleep(5)
            if not api.status().get("worker_environment_exists"):  # (a restarted manager may keep it)
                api.environment_open()
            api.wait_for_idle(timeout=120)
            print(f"RE environment open: {api.status()['worker_environment_exists']}")
        print(f"control host running - dashboards http://localhost:{a.web_port}, data server "
              f"http://<this host>:{a.tiled_port} (key in {key_file}), queue tcp://<this host>:60615. "
              f"Logs: {data_dir}. Ctrl-C to stop.", flush=True)
        while True:
            for entry in procs:
                name, p, log, cmd = entry
                if p.poll() is None:
                    continue
                if name not in RESTARTABLE:
                    raise SystemExit(f"{name} exited ({p.returncode}) - see {data_dir / (name + '.log')}")
                print(f"{name} exited ({p.returncode}) - restarting", flush=True)
                entry[1] = subprocess.Popen(cmd, env=env, cwd=CONTROL, stdout=log, stderr=subprocess.STDOUT)
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        for _, p, log, _ in reversed(procs):
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
            log.close()


if __name__ == "__main__":
    main()
