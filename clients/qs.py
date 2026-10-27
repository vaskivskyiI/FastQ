"""Queue client - control experiments from ANY PC (thin wrapper around
bluesky-queueserver-api; the standard `qserver` CLI works too).

    python clients/qs.py status
    python clients/qs.py plans                                  # what can be queued
    python clients/qs.py add rt_sweep start_k=10 final_k=2 time_min=30
    python clients/qs.py add iv_curves "temperatures_k=[2, 4, 6]"
    python clients/qs.py start | stop | abort | queue | history | env-open | env-close
    python clients/qs.py watch                                  # console output of the running plan

Control host: --host (default: env FASTQ_CONTROL_HOST or localhost). From
outside the lab LAN tunnel the two queue ports over SSH first (see README):
    ssh -N -L 8088:127.0.0.1:8088 -L 60615:127.0.0.1:60615 -L 60625:127.0.0.1:60625 -L 8000:127.0.0.1:8000 <lab-ssh-host>
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys

from bluesky_queueserver_api import BPlan
from bluesky_queueserver_api.zmq import REManagerAPI


def parse_value(raw: str):
    """1e-5 -> float, [2, 4] -> list, true -> True, anything else -> str."""
    for parse in (json.loads, ast.literal_eval):
        try:
            return parse(raw)
        except (ValueError, SyntaxError):
            pass
    return raw


def parse_kwargs(items: list[str]) -> dict:
    return {k: parse_value(v) for k, _, v in (item.partition("=") for item in items)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--host", default=os.environ.get("FASTQ_CONTROL_HOST", "localhost"))
    a = ap.parse_args()
    api = REManagerAPI(zmq_control_addr=f"tcp://{a.host}:60615", zmq_info_addr=f"tcp://{a.host}:60625")
    c = a.command
    if c == "status":
        s = api.status()
        for k in ("manager_state", "worker_environment_exists", "items_in_queue", "items_in_history",
                  "running_item_uid", "re_state"):
            print(f"{k:28s} {s.get(k)}")
    elif c == "plans":
        for name, spec in sorted(api.plans_allowed()["plans_allowed"].items()):
            params = ", ".join(p["name"] for p in spec.get("parameters", []))
            print(f"{name:28s} ({params})")
    elif c == "add":
        if not a.args:
            sys.exit("usage: add <plan> key=value ...")
        print(api.item_add(BPlan(a.args[0], **parse_kwargs(a.args[1:]))))
    elif c == "start":
        print(api.queue_start())
    elif c == "stop":
        print(api.queue_stop())            # after the current plan
    elif c == "abort":
        print(api.re_pause(option="immediate"), api.re_abort())
    elif c == "queue":
        print(json.dumps(api.queue_get()["items"], indent=1))
    elif c == "history":
        for item in api.history_get()["items"][-20:]:
            print(item["name"], item.get("kwargs"), item.get("result", {}).get("exit_status"))
    elif c == "env-open":
        print(api.environment_open())
    elif c == "env-close":
        print(api.environment_close())
    elif c == "watch":
        api.console_monitor.enabled = True
        try:
            while True:
                try:
                    print(api.console_monitor.next_msg(timeout=1)["msg"], end="", flush=True)
                except api.RequestTimeoutError:
                    pass
        except KeyboardInterrupt:
            pass
    else:
        sys.exit(f"unknown command {c!r}")


if __name__ == "__main__":
    main()
