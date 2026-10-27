"""FastQ web dashboards - one web server on the control host, any browser in the lab.

    python control/web/server.py                  # http://<control host>:8088  (lab.yaml)
    python control/web/server.py --lab control/lab_sim.yaml --port 8088

Pages (static/pages.js): Control room (live run + plots), Experiments (plans, scan
builder, presets, queue), Runs (search, plots, frames, export, compare), Devices,
Logbook (ELOG), System. Run data and plots come from fastq.analysis, the live run
from the RunEngine's live file (fastq.live). Started by start_control_host.py.

The browser never speaks Channel Access / ZMQ: this server is the gateway
(caproto client, bluesky-queueserver-api, tiled client) and pushes live PV
values over a websocket, so it also works through a single SSH tunnel port.

Safety: PV writes only on an explicit click, only for the controls of a device's
profile (IOC schema or clients/device_profiles/*.json, plus IOC:SYSRESET), never while the queue is
running a plan, and not at all with --read-only. Each control has a safety level (fastq.security):
safe / confirm (the page asks) / protected (the lab PIN, checked here).
History: every numeric value of every device is recorded (fastq.history) - trends load it, so
they survive page changes and restarts. Device page layouts are saved in <data>/views.json.
Access: user accounts (fastq.users: viewer / operator / admin, `python -m fastq.users add <name>`);
without any account the dashboard is open. Every change is in <data>/audit.jsonl. Screen capture in the browser needs a secure origin: open the
page as http://localhost:8088 (on the host or through the SSH tunnel) or run
with --ssl-certfile/--ssl-keyfile; elsewhere paste a screenshot (Win+Shift+S,
Ctrl+V) or attach a file.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import math
import os
import secrets
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from pathlib import Path
from urllib.parse import urlparse

CONTROL = Path(__file__).resolve().parents[1]
REPO = CONTROL.parent
sys.path.insert(0, str(CONTROL))

import numpy as np  # noqa: E402
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from fastq import elog as E  # noqa: E402
from fastq import alarms as AL  # noqa: E402
from fastq import analysis as A  # noqa: E402
from fastq import derived as DV  # noqa: E402
from fastq import history as H  # noqa: E402
from fastq import security as SEC  # noqa: E402
from fastq import users as U  # noqa: E402
from fastq.experiment import get_state, set_state  # noqa: E402
from fastq.live import read_live  # noqa: E402
from fastq.profiles import profile_from_schema  # noqa: E402
from fastq.roles import ROLE_INTERFACES  # noqa: E402
from fastq.session import backup_dir, data_root, load_lab  # noqa: E402
from fastq.session import tiled_dir as tiled_dir_of  # noqa: E402

log = logging.getLogger("fastq.web")
SCHEMA_TTL_S = 120.0
LIVE_RUN_MAX_IDLE_S = 3600.0  # a live file without a stop for this long = a crashed session, not a run
PUT_WAIT_MAX_S = 20.0         # longer actions are started without waiting for their completion
STATIC = Path(__file__).resolve().parent / "static"
PROFILES = REPO / "clients" / "device_profiles"
MAX_ARRAY = 2000


def _clean(v):
    """JSON-safe scalar."""
    if isinstance(v, (np.generic,)):
        v = v.item()
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, bytes):
        return v.decode(errors="replace")
    return v


# ------------------------------------------------------------------ Channel Access gateway
class PVHub:
    """Shared caproto subscriptions, fanned out to websocket clients."""

    def __init__(self) -> None:
        from caproto.threading.client import Context

        self.ctx = Context()
        self.lock = threading.Lock()
        self.entries: dict[str, dict] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    # --- decoding
    @staticmethod
    def _decode(entry: dict, data):
        from caproto import ChannelType

        native = entry.get("native")
        if native == ChannelType.ENUM:
            idx = int(data[0])
            strings = entry.get("enum_strings") or []
            return strings[idx] if idx < len(strings) else idx
        if native == ChannelType.STRING:
            vals = [d.decode(errors="replace") if isinstance(d, bytes) else str(d) for d in data]
            return vals[0] if len(vals) == 1 else vals
        if native == ChannelType.CHAR and entry.get("count", 1) > 1:
            return bytes(bytearray(np.asarray(data, dtype=np.uint8))).split(b"\x00", 1)[0].decode(errors="replace")
        if len(data) == 1:
            return _clean(data[0])
        arr = np.asarray(data)
        if arr.size > MAX_ARRAY:
            arr = arr[:: int(math.ceil(arr.size / MAX_ARRAY))]
        return [_clean(x) for x in arr.tolist()]

    # --- subscriptions
    def _entry(self, name: str) -> tuple[dict, bool]:
        """The subscription of `name` (created on first use; call with self.lock held), and whether it is new."""
        entry = self.entries.get(name)
        if entry is not None:
            return entry, False
        entry = self.entries[name] = {"name": name, "clients": set(), "connected": False, "value": None,
                                      "ts": None, "units": "", "precision": None}
        (pv,) = self.ctx.get_pvs(name, connection_state_callback=self._conn_cb, timeout=2.0)
        entry["pv"] = pv
        return entry, True

    def watch(self, name: str, client) -> None:
        with self.lock:
            entry, _ = self._entry(name)
            entry["clients"].add(client)
            snapshot = self._payload(entry)
        client.push(snapshot)

    def peek_many(self, names, wait: float = 1.5) -> dict:
        """{name: latest value, or an exception} from standing subscriptions. Only PVs seen for the first
        time are waited for (together, at most `wait` s); a PV that is down answers at once afterwards -
        an IOC that is not running costs nothing, and its values arrive when it starts."""
        with self.lock:
            entries = {n: self._entry(n) for n in dict.fromkeys(names)}
        fresh = [e for e, new in entries.values() if new]
        end = time.monotonic() + wait
        while fresh and time.monotonic() < end and any(e["ts"] is None for e in fresh):
            time.sleep(0.05)
        return {n: e["value"] if e["connected"] and e["ts"] is not None else TimeoutError(f"{n} not connected")
                for n, (e, _) in entries.items()}

    def peek(self, name: str, wait: float = 1.5):
        value = self.peek_many([name], wait)[name]
        if isinstance(value, Exception):
            raise value
        return value

    def unwatch(self, name: str, client) -> None:
        with self.lock:
            entry = self.entries.get(name)
            if entry:
                entry["clients"].discard(client)  # subscription kept: re-opening a page is instant

    def _payload(self, entry: dict) -> dict:
        return {k: entry.get(k) for k in ("name", "connected", "value", "ts", "units", "precision", "enum_strings")}

    def _publish(self, entry: dict) -> None:
        payload = self._payload(entry)
        for c in list(entry["clients"]):
            c.push(payload)

    def _conn_cb(self, pv, state: str) -> None:
        entry = self.entries.get(pv.name)
        if entry is None:
            return
        entry["connected"] = state == "connected"
        if entry["connected"] and "sub" not in entry:
            threading.Thread(target=self._setup, args=(entry,), daemon=True).start()
        self._publish(entry)

    def _setup(self, entry: dict) -> None:
        pv = entry["pv"]
        try:
            entry["native"] = pv.channel.native_data_type
            entry["count"] = pv.channel.native_data_count
            md = pv.read(data_type="control", timeout=3).metadata
            entry["units"] = _clean(getattr(md, "units", b"")) or ""
            entry["precision"] = getattr(md, "precision", None)
            if hasattr(md, "enum_strings"):
                entry["enum_strings"] = [_clean(s) for s in md.enum_strings]
        except Exception as exc:  # noqa: BLE001
            log.debug("metadata %s: %s", pv.name, exc)
        sub = pv.subscribe(data_type="time")

        def cb(sub, response):
            entry["value"] = self._decode(entry, response.data)
            entry["ts"] = getattr(response.metadata, "timestamp", time.time())
            self._publish(entry)

        entry["sub"], entry["cb"] = sub, cb
        sub.add_callback(cb)

    # --- one-shot access
    def read(self, name: str, timeout: float = 3.0, fresh: bool = False):
        with self.lock:
            entry = self.entries.get(name)
        if not fresh and entry and entry.get("sub") and entry["value"] is not None:
            return entry["value"]
        (pv,) = self.ctx.get_pvs(name, timeout=timeout)
        pv.wait_for_connection(timeout=timeout)
        tmp = {"native": pv.channel.native_data_type, "count": pv.channel.native_data_count}
        if tmp["native"].name == "ENUM":
            tmp["enum_strings"] = [_clean(s) for s in pv.read(data_type="control", timeout=timeout).metadata.enum_strings]
        return self._decode(tmp, pv.read(timeout=timeout).data)

    def write(self, name: str, value, timeout: float = 5.0, wait: bool = True) -> None:
        (pv,) = self.ctx.get_pvs(name, timeout=5.0)
        pv.wait_for_connection(timeout=5.0)
        # SYSRESET: the IOC exits before it could confirm the put
        wait = wait and not name.endswith(":IOC:SYSRESET")
        pv.write([value] if not isinstance(value, (list, tuple)) else value, wait=wait, timeout=timeout)


class WSClient:
    """Per-websocket buffer: latest value per PV, flushed a few times a second."""

    def __init__(self, hub: PVHub, loop: asyncio.AbstractEventLoop) -> None:
        self.hub, self.loop = hub, loop
        self.pending: dict[str, dict] = {}
        self.event = asyncio.Event()
        self.names: set[str] = set()

    def push(self, payload: dict) -> None:
        def _set():
            self.pending[payload["name"]] = payload
            self.event.set()
        self.loop.call_soon_threadsafe(_set)


# ------------------------------------------------------------------ application
def create_app(lab: dict, read_only: bool = False) -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        await startup()
        yield

    app = FastAPI(title="FastQ dashboards", docs_url="/api/docs", lifespan=lifespan)
    hub = PVHub()
    state: dict = {"api": None, "console": [], "console_clients": set(), "tiled": None, "tiled_err": None}
    data_cfg = lab.get("data") or {}
    elog_cfg = E.elog_config(lab)
    directories = [p.strip() for p in (os.environ.get("FASTQ_DIRECTORY") or lab.get("directory") or "FASTQ:DIR:").split(",")
                   if p.strip()]
    control_host = os.environ.get("FASTQ_CONTROL_HOST", "localhost")
    READ_ONLY_POSTS = ("/plot",)  # POSTs that only read (a plot spec in the body)
    PUBLIC = ("/api/login", "/api/logout", "/api/me")
    ADMIN = ("/api/users", "/api/history/settings", "/api/derived")  # changes here need an admin
    OPEN = {"name": "lab", "role": "admin", "display": "lab"}       # no accounts: the dashboard is open

    def current_user(cookie: str | None) -> dict | None:
        """The logged-in user; OPEN while the lab has no accounts (fastq.users)."""
        return U.session_user(lab, cookie) if U.load(lab) else OPEN

    @app.middleware("http")
    async def auth(request: Request, call_next):
        path, method = request.url.path, request.method
        changes = method in ("POST", "PUT", "DELETE", "PATCH") and not path.endswith(READ_ONLY_POSTS)
        if path.startswith("/api/") and path not in PUBLIC:
            user = await asyncio.to_thread(current_user, request.cookies.get("fastq_session"))
            if user is None:
                return JSONResponse({"detail": "login required"}, status_code=401)
            need = "admin" if changes and path.startswith(ADMIN) else "operator" if changes else "viewer"
            if not U.allowed(user["role"], need):
                return JSONResponse({"detail": f"{user['name']} is {user['role']}: needs {need}"}, status_code=403)
            request.state.user = user
        # --read-only: nothing that changes the lab, the queue, ELOG or saved settings
        if read_only and changes:
            return JSONResponse({"detail": "dashboard runs --read-only"}, status_code=403)
        response = await call_next(request)
        if changes and path not in PUBLIC:  # who changed what (the PV writes add their details)
            who = getattr(request.state, "user", OPEN)["name"]
            await asyncio.to_thread(U.audit, lab, who, f"{method} {path}", None, response.status_code < 400)
        return response

    def ws_user(ws: WebSocket) -> dict | None:
        return current_user(ws.cookies.get("fastq_session"))

    @app.post("/api/login")
    async def login(body: dict):
        user = await asyncio.to_thread(U.check_password, lab, body.get("user", ""), body.get("password", ""))
        if user is None:
            await asyncio.sleep(1.0)  # slows guessing
            U.audit(lab, body.get("user", "?"), "login", ok=False)
            raise HTTPException(401, "wrong user or password")
        U.audit(lab, user["name"], "login")
        r = JSONResponse(user)
        r.set_cookie("fastq_session", U.make_session(lab, user["name"]), max_age=U.SESSION_S, httponly=True,
                     samesite="lax")
        return r

    @app.post("/api/logout")
    async def logout():
        r = JSONResponse({"ok": True})
        r.delete_cookie("fastq_session")
        return r

    @app.get("/api/me")
    async def me(request: Request):
        accounts = bool(await asyncio.to_thread(U.load, lab))
        return {"user": await asyncio.to_thread(current_user, request.cookies.get("fastq_session")),
                "accounts": accounts}

    @app.get("/api/users")
    async def users_list():
        return [U.public(n, u) for n, u in sorted((await asyncio.to_thread(U.load, lab)).items())]

    @app.put("/api/users/{name}")
    async def users_put(name: str, body: dict):
        try:
            return await asyncio.to_thread(U.set_user, lab, name, body.get("password") or None, body.get("role"),
                                           body.get("display"))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.delete("/api/users/{name}")
    async def users_delete(name: str):
        try:
            await asyncio.to_thread(U.remove_user, lab, name)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}

    @app.get("/api/audit")
    async def audit_log(n: int = 200, user: str = "", text: str = ""):
        return await asyncio.to_thread(U.audit_tail, lab, min(n, 2000), user, text)

    async def startup():
        from bluesky_queueserver_api.zmq.aio import REManagerAPI

        hub.loop = asyncio.get_running_loop()
        threading.Thread(target=history_channels, daemon=True, name="history-channels").start()
        threading.Thread(target=alarm_channels, daemon=True, name="alarm-channels").start()
        state["api"] = REManagerAPI(zmq_control_addr=f"tcp://{control_host}:60615",
                                    zmq_info_addr=f"tcp://{control_host}:60625", timeout_recv=3.0)
        asyncio.create_task(console_task())

    # ---------------------------------------------------------------- static / config
    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/lab")
    async def lab_info():
        return {"directories": directories, "roles": lab.get("roles") or {}, "metadata": lab.get("metadata") or {},
                "tiled_uri": data_cfg.get("tiled_uri"), "read_only": read_only, "lab_file": lab.get("_path"),
                "host": socket.gethostname(),
                # plan parameters named like these are devices (the plan form offers the device list)
                "role_names": sorted(set(lab.get("roles") or {}) | set(ROLE_INTERFACES) | {"probe"}),
                "security": {"pin": bool(SEC.pin_hash(lab)),
                             "overrides": (lab.get("security") or {}).get("overrides") or {}},
                "experiment": get_state(lab),
                "elog": {"url": elog_cfg.get("url"), "enabled": bool(elog_cfg.get("url")),
                         "device_logbooks": elog_cfg.get("device_logbooks") or {},
                         "attributes": E.attribute_names(lab)}}

    @app.get("/api/experiment")
    async def experiment_get():
        return get_state(lab)

    @app.post("/api/experiment")
    async def experiment_set(body: dict):
        changes = {k: body[k] for k in ("sample", "notebook") if k in body}
        if not changes:
            raise HTTPException(400, "nothing to change (sample, notebook)")
        state = set_state(lab, **changes)
        log.info("experiment changed: %s", changes)
        return state

    schema_cache: dict[str, dict] = {}
    schema_time: dict[str, float] = {}

    def _stale(prefix: str) -> bool:
        """Schemas are re-read after SCHEMA_TTL_S: a restarted IOC may publish new params."""
        return prefix not in schema_cache or time.monotonic() - schema_time.get(prefix, 0) > SCHEMA_TTL_S

    def _refresh_schemas(devs: list[dict]) -> None:
        """Schemas of the enabled devices from standing subscriptions (a restarted IOC's new schema
        arrives by itself); a device that is down keeps its last schema, if any, and costs no waiting."""
        stale = [d["prefix"] for d in devs if d.get("enabled") and _stale(d["prefix"])]
        for prefix, value in hub.peek_many([p + "IOC:SCHEMA" for p in stale]).items():
            if isinstance(value, Exception):
                continue
            try:
                schema_cache[prefix[:-len("IOC:SCHEMA")]] = json.loads(value)
                schema_time[prefix[:-len("IOC:SCHEMA")]] = time.monotonic()
            except ValueError:
                log.warning("%s: unreadable schema", prefix)

    def _profiles() -> dict:
        """Hand-written clients/device_profiles/<type>.json, else generated from the IOC's schema."""
        out = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in PROFILES.glob("*.json")}
        try:
            devs = _directory()["devices"]
        except Exception:  # noqa: BLE001
            devs = []
        _refresh_schemas([d for d in devs if d["type"] not in out])
        for d in devs:
            if d["type"] in out or not d.get("enabled"):
                continue
            if d["prefix"] not in schema_cache:
                continue
            out[d["type"]] = profile_from_schema(schema_cache[d["prefix"]])
        return out

    @app.get("/api/profiles")
    async def profiles():
        return await asyncio.to_thread(_profiles)

    @app.get("/api/schemas")
    async def all_schemas():
        """{device name: schema} of every instrument (scan builder: axes, detectors)."""
        def collect():
            devs = _directory()["devices"]
            _refresh_schemas(devs)  # every instance (the profiles only need one per type)
            names = {d["prefix"]: d["name"] for d in devs}
            return {names[p]: s for p, s in schema_cache.items() if p in names}
        return await asyncio.to_thread(collect)

    @app.get("/api/schema/{name}")
    async def device_schema(name: str):
        """The self-description of a declarative instrument (params, units, docs, actions)."""
        dev = next((d for d in (await asyncio.to_thread(_directory))["devices"] if d["name"] == name), None)
        if dev is None:
            raise HTTPException(404, f"no device {name}")
        try:
            return json.loads(await asyncio.to_thread(hub.peek, dev["prefix"] + "IOC:SCHEMA"))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(404, f"{name} has no schema ({type(exc).__name__})")

    # ---------------------------------------------------------------- devices / PVs
    def _directory():
        merged, hosts, errors = {}, {}, []
        got = hub.peek_many([d + pv for d in directories for pv in ("AllDevices", "Devices", "Host", "Peers")])
        for d in directories:
            for pv in ("AllDevices", "Devices"):
                if isinstance(got[d + pv], Exception):
                    continue
                try:
                    for e in json.loads(got[d + pv]):
                        merged.setdefault(e["prefix"], e)
                    break
                except ValueError:
                    log.warning("%s%s: not a device list", d, pv)
            else:  # neither list readable: that IOC host (its directory IOC) is not running
                errors.append(d)
            for pv in ("Host", "Peers"):
                if not isinstance(got[d + pv], Exception):
                    hosts.setdefault(d, {})[pv] = got[d + pv]
        dp = (lab.get("derived") or {}).get("prefix")
        if dp and dp not in merged:  # the derived-values IOC runs on the control host, outside any directory
            merged[dp] = {"prefix": dp, "type": "derived", "enabled": True, "host": "control host"}
        devices = sorted(merged.values(), key=lambda e: (e.get("host", ""), e["prefix"]))
        for e in devices:
            e["name"] = e["prefix"].rstrip(":").split(":")[-1].lower()
        return {"devices": devices, "directories": hosts, "errors": errors}  # errors = unreachable directories

    @app.get("/api/devices")
    async def devices():
        return await asyncio.to_thread(_directory)

    def _values(name: str) -> dict:
        dev = next((d for d in _directory()["devices"] if d["name"] == name), None)
        if dev is None:
            raise HTTPException(404, f"no device {name}")
        prof = _profiles().get(dev["type"]) or {"fields": [{"suffix": "Connected"}]}
        got = hub.peek_many([dev["prefix"] + f["suffix"] for f in prof["fields"]])
        out = {f["suffix"]: None if isinstance(v, Exception) else v
               for f, v in zip(prof["fields"], got.values())}
        return {"name": name, "type": dev["type"], "prefix": dev["prefix"], "host": dev.get("host"), "values": out}

    @app.get("/api/devices/{name}/values")
    async def device_values(name: str):
        """Current value of every profile field of a device (arrays decimated to MAX_ARRAY points)."""
        return await asyncio.to_thread(_values, name)

    @app.websocket("/ws/pv")
    async def ws_pv(ws: WebSocket):
        if ws_user(ws) is None:
            await ws.close(code=4401)
            return
        await ws.accept()
        client = WSClient(hub, asyncio.get_running_loop())

        async def sender():
            while True:
                await client.event.wait()
                client.event.clear()
                batch, client.pending = list(client.pending.values()), {}
                await ws.send_text(json.dumps(batch, default=str))
                await asyncio.sleep(0.2)

        task = asyncio.create_task(sender())
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                for name in msg.get("sub", []):
                    if name not in client.names and len(client.names) < 2000:
                        client.names.add(name)
                        await asyncio.to_thread(hub.watch, name, client)
                for name in msg.get("unsub", []):
                    client.names.discard(name)
                    hub.unwatch(name, client)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            task.cancel()
            for name in client.names:
                hub.unwatch(name, client)

    async def _queue_busy() -> str | None:
        """Why instrument writes are refused now (None = allowed). Fails closed: an unreachable queue
        server, or a run in the live file (also from an interactive session), counts as busy."""
        try:
            s = await state["api"].status()
        except Exception as exc:  # noqa: BLE001
            return f"queue server state unknown ({type(exc).__name__})"
        if s.get("manager_state") not in ("idle", None) or s.get("re_state") not in ("idle", None):
            return f"queue server is {s.get('manager_state')} / RE {s.get('re_state')}"
        live = read_live(lab) or {}
        last = max(live.get("last_event_time") or 0, live.get("start_time") or 0)
        if live.get("uid") and live.get("exit_status") is None and time.time() - last < LIVE_RUN_MAX_IDLE_S:
            return f"run {live.get('scan_id')} ({live.get('plan_name')}) is running"
        return None

    def _control_level(pv: str) -> str | None:
        """Safety level of a dashboard control, None = not a control."""
        if pv.endswith(":IOC:SYSRESET"):
            return "confirm"
        devs = {d["prefix"]: d["name"] for d in _directory()["devices"]}
        prefix = next((p for p in sorted(devs, key=len, reverse=True) if pv.startswith(p)), None)
        if prefix is None:
            return None
        dev = next(d for d in _directory()["devices"] if d["prefix"] == prefix)
        prof = _profiles().get(dev["type"]) or {}
        for c in prof.get("controls", []):
            if prefix + c.get("write_suffix", "") == pv:
                return SEC.level(lab, devs[prefix], c["write_suffix"], c.get("safety"))
        return None

    @app.post("/api/pv/put")
    async def pv_put(body: dict, request: Request):
        pv, value = body.get("pv", ""), body.get("value")
        if read_only:
            raise HTTPException(403, "dashboard runs --read-only")
        lvl = await asyncio.to_thread(_control_level, pv)
        if lvl is None:
            raise HTTPException(403, f"{pv} is not a dashboard control (see clients/device_profiles)")
        user = getattr(request.state, "user", OPEN)
        if lvl == "protected":
            try:
                if U.load(lab):  # accounts: the user's own password - the audit log names who did it
                    if not U.check_password(lab, user["name"], body.get("pin") or ""):
                        raise PermissionError("protected control: your password is needed" if not body.get("pin")
                                              else "wrong password")
                else:
                    SEC.check_pin(lab, body.get("pin"))
            except PermissionError as exc:
                U.audit(lab, user["name"], "pv put refused", {"pv": pv, "value": value, "why": str(exc)}, ok=False)
                raise HTTPException(403, str(exc))
        busy = await _queue_busy()
        if busy:
            raise HTTPException(409, f"refused while a plan runs ({busy})")
        timeout = await asyncio.to_thread(_action_timeout, pv)
        started = timeout is not None and timeout > PUT_WAIT_MAX_S
        try:  # long actions (homing, a Keithley run, output ramps): started here, followed via Busy/LastError
            await asyncio.to_thread(hub.write, pv, value, max(timeout or 0, 5.0), not started)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"{type(exc).__name__}: {exc}")
        log.info("web put %s = %r (%s) by %s", pv, value, lvl, user["name"])
        U.audit(lab, user["name"], "pv put", {"pv": pv, "value": value, "safety": lvl, "via": body.get("who")})
        return {"ok": True, "safety": lvl, "started": started}

    def _action_timeout(pv: str) -> float | None:
        for prefix, schema in schema_cache.items():
            if pv.startswith(prefix):
                a = next((a for a in schema.get("actions", []) if prefix + a["name"] == pv), None)
                return float(a.get("timeout_s") or 120.0) if a else None
        return None

    # ---------------------------------------------------------------- history, views, derived values
    recorder = H.Recorder(lab)

    # ---------------------------------------------------------------- alarms (fastq.alarms)
    alarm_events: list = []

    def alarm_post(e: dict) -> None:
        r = e["rule"]
        text = (f"{r['name']}: {'ALARM' if e['kind'] == 'active' else 'cleared'} - {r['ref']} {e['why']}"
                + (f"\n{r['note']}" if r["note"] and e["kind"] == "active" else ""))
        alarm_events.append({"t": e["t"], "name": r["name"], "kind": e["kind"], "text": text,
                             "notify": "notify" in r["actions"]})
        del alarm_events[:-200]
        U.audit(lab, "alarm", e["kind"], {"rule": r["name"], "ref": r["ref"], "value": e["value"], "why": e["why"]})
        if "elog" in r["actions"] and elog_cfg.get("url"):
            E.post_entry(lab, r["logbook"] or E.current_experiment(lab), text.splitlines()[0],
                         f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e['t']))}\n{text}",
                         {"category": E.auto_category(lab, "device_event")}, kind="alarm")

    alarms = AL.Engine(AL.load(lab), alarm_post)
    alarm_watch: dict = {}   # pv -> ref

    def _ref_pv(ref: str) -> str | None:
        dev_name, _, attr = ref.partition(".")
        dev = next((d for d in _directory()["devices"] if d["name"] == dev_name), None)
        if dev is None:
            return None
        _refresh_schemas([dev])
        if dev["prefix"] not in schema_cache:
            return None
        p = next((p for p in schema_cache[dev["prefix"]].get("params", []) if p["attr"] == attr), None)
        return dev["prefix"] + p["name"] + ("_RBV" if p.get("rbv") else "") if p else None

    class _AlarmClient:
        def __init__(self, ref: str) -> None:
            self.ref = ref

        def push(self, payload: dict) -> None:
            if payload.get("connected"):
                alarms.update(self.ref, payload.get("value"), payload.get("ts"))

    def alarm_channels() -> None:
        """Subscribe the PV of every rule's ref (re-checked: rules change, devices appear)."""
        while True:
            try:
                for ref in alarms.refs():
                    if ref in alarm_watch.values():
                        continue
                    pv = _ref_pv(ref)
                    if pv:
                        alarm_watch[pv] = ref
                        hub.watch(pv, _AlarmClient(ref))
            except Exception:  # noqa: BLE001
                log.exception("alarm channel scan failed")
            time.sleep(20)

    @app.get("/api/alarms")
    async def alarms_get(since: float = 0.0):
        return {"rules": alarms.status(), "events": [e for e in alarm_events if e["t"] > since],
                "active": sum(1 for r in alarms.status() if r["active"])}

    @app.put("/api/alarms")
    async def alarms_put(body: dict):
        try:
            rules = await asyncio.to_thread(AL.save, lab, body.get("rules", []))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        alarms.set_rules(rules)
        return {"rules": alarms.status()}

    class _RecClient:
        @staticmethod
        def push(payload: dict) -> None:
            if payload.get("connected"):
                recorder.add(payload["name"], payload.get("value"), payload.get("ts"))

    def history_channels() -> None:
        """Record every numeric readable param of every enabled device; re-checked every minute."""
        while True:
            try:
                devs = [d for d in _directory()["devices"] if d.get("enabled")]
                _profiles()  # fills schema_cache
                for d in devs:
                    schema = schema_cache.get(d["prefix"])
                    for prm in (schema or {}).get("params", []):
                        if prm["dtype"] not in ("float", "int", "bool") or not prm.get("readable", True):
                            continue
                        pv = d["prefix"] + prm["name"] + ("_RBV" if prm.get("rbv") else "")
                        if pv not in recorder.channels():
                            recorder.channel(pv, d["name"], prm.get("units") or "")
                            hub.watch(pv, _RecClient)
            except Exception:  # noqa: BLE001
                log.exception("history channel scan failed")
            time.sleep(60)

    @app.get("/api/history")
    async def history(pv: str, start: float | None = None, end: float | None = None, points: int = 1500):
        end = end or time.time()
        start = start if start is not None else end - 3600
        return await asyncio.to_thread(recorder.query, pv, start, end, min(max(points, 10), 10000))

    @app.get("/api/history/settings")
    async def history_settings():
        return {**recorder.settings, "stats": await asyncio.to_thread(recorder.stats)}

    @app.put("/api/history/settings")
    async def history_settings_put(body: dict):
        try:
            saved = H.save_settings(lab, body)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        recorder.reload_settings()
        return saved

    views_file = data_root(lab) / "views.json"

    def _views() -> dict:
        try:
            return json.loads(views_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"devices": {}}

    @app.get("/api/views/device/{name}")
    async def device_view(name: str):
        return _views().get("devices", {}).get(name) or {"graphs": []}

    @app.put("/api/views/device/{name}")
    async def device_view_put(name: str, body: dict):
        graphs = []
        for g in body.get("graphs", [])[:20]:
            graphs.append({"id": str(g.get("id") or secrets.token_hex(4)), "title": str(g.get("title", ""))[:80],
                           "pvs": [str(x) for x in g.get("pvs", [])][:12], "window_s": float(g.get("window_s") or 3600),
                           "logY": bool(g.get("logY"))})
        v = _views()
        v.setdefault("devices", {})[name] = {"graphs": graphs}
        tmp = views_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(v, indent=1), encoding="utf-8")
        tmp.replace(views_file)
        return {"graphs": graphs}

    @app.get("/api/derived")
    async def derived_get():
        defs = await asyncio.to_thread(DV.load, lab)
        return {"values": defs, "prefix": (lab.get("derived") or {}).get("prefix"),
                "functions": sorted(DV.FUNCS), "constants": sorted(DV.CONSTS)}

    @app.put("/api/derived")
    async def derived_put(body: dict):
        if read_only:
            raise HTTPException(403, "dashboard runs --read-only")
        try:
            defs = await asyncio.to_thread(DV.save, lab, body.get("values", []))
        except (ValueError, SyntaxError) as exc:
            raise HTTPException(400, f"{type(exc).__name__}: {exc}")
        schema_time.pop((lab.get("derived") or {}).get("prefix"), None)  # the IOC restarts with new PVs
        log.info("derived values changed: %s", [d["name"] for d in defs])
        return {"values": defs}

    # ---------------------------------------------------------------- queue server
    async def qcall(method: str, *args, **kwargs):
        api = state["api"]
        try:
            return await getattr(api, method)(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"queue server: {type(exc).__name__}: {exc}")

    @app.get("/api/queue/status")
    async def q_status():
        s = await qcall("status")
        try:
            s["active_runs"] = (await state["api"].re_runs(option="active"))
        except Exception:  # noqa: BLE001
            s["active_runs"] = []
        return s

    @app.get("/api/queue/items")
    async def q_items():
        q = await qcall("queue_get")
        h = await qcall("history_get")
        h["items"] = h.get("items", [])[-30:]
        return {"queue": q, "history": h}

    @app.get("/api/queue/plans")
    async def q_plans():
        return await qcall("plans_allowed")

    @app.get("/api/queue/devices")
    async def q_devices():
        d = await qcall("devices_allowed")
        return {"devices": sorted(d.get("devices_allowed") or {})}

    @app.post("/api/queue/add")
    async def q_add(body: dict, request: Request):
        from bluesky_queueserver_api import BPlan

        # the queue server knows devices by NAME (mfli1); accept PV prefixes (FASTQ:MFLI1:) too
        prefixes = {}
        try:
            prefixes = {e["prefix"]: e["name"] for e in (await asyncio.to_thread(_directory))["devices"]}
        except Exception:  # noqa: BLE001
            pass

        def to_name(v):
            if isinstance(v, str) and v in prefixes:
                return prefixes[v]
            if isinstance(v, list):
                return [to_name(x) for x in v]
            return v

        kwargs = {k: to_name(v) for k, v in (body.get("kwargs") or {}).items()}
        args = [to_name(v) for v in (body.get("args") or [])]
        item = BPlan(body["name"], *args, **kwargs)
        user = getattr(request.state, "user", OPEN)  # the queue history says who queued it
        return await qcall("item_add", item, pos=body.get("pos", "back"), user=user["display"])

    @app.post("/api/queue/remove")
    async def q_remove(body: dict):
        return await qcall("item_remove", uid=body["uid"])

    @app.post("/api/queue/reload_plans")
    async def q_reload_plans():
        """Re-open the RE environment: re-imports control/experiments/*.py and re-discovers devices."""
        api = state["api"]
        busy = await _queue_busy()
        if busy:
            raise HTTPException(409, f"not while the queue is busy ({busy})")
        try:
            if (await api.status()).get("worker_environment_exists"):
                await api.environment_close()
                await api.wait_for_idle(timeout=120)
            await api.environment_open()
            await api.wait_for_idle(timeout=300)
            n = len((await api.plans_allowed()).get("plans_allowed") or {})
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"queue server: {type(exc).__name__}: {exc}")
        return {"plans": n}

    @app.post("/api/queue/move")
    async def q_move(body: dict):
        """Reorder: {uid, before_uid | after_uid | pos ("front" / "back")}."""
        kw = {k: body[k] for k in ("before_uid", "after_uid", "pos") if body.get(k)}
        return await qcall("item_move", uid=body["uid"], **kw)

    @app.post("/api/run/{action}")
    async def run_end(action: str):
        """End the running run: stop (success) or abort. The queue server only ends a PAUSED run, so
        a running one is paused (immediately) first - one place for the dashboard and the AI tools."""
        if action not in ("stop", "abort"):
            raise HTTPException(404, action)
        s = await qcall("status")
        if s.get("re_state") == "running":
            await qcall("re_pause", option="immediate")
            for _ in range(60):
                await asyncio.sleep(0.25)
                if (await qcall("status")).get("re_state") == "paused":
                    break
            else:
                raise HTTPException(504, "the run did not pause within 15 s")
        return await qcall("re_stop" if action == "stop" else "re_abort")

    @app.post("/api/queue/{action}")
    async def q_action(action: str, body: dict | None = None):
        methods = {"start": "queue_start", "stop": "queue_stop", "stop_cancel": "queue_stop_cancel",
                   "pause": "re_pause", "resume": "re_resume", "abort": "re_abort", "halt": "re_halt",
                   "re_stop": "re_stop", "env_open": "environment_open", "env_close": "environment_close",
                   "clear": "queue_clear", "history_clear": "history_clear"}
        if action not in methods:
            raise HTTPException(404, action)
        kwargs = {"option": (body or {}).get("option", "deferred")} if action == "pause" else {}
        return await qcall(methods[action], **kwargs)

    async def console_task():
        api = state["api"]
        api.console_monitor.enable()
        while True:
            try:
                msg = await api.console_monitor.next_msg(timeout=1.0)
            except Exception:  # timeout / manager down
                await asyncio.sleep(0.2)
                continue
            text = msg.get("msg", "")
            state["console"].append(text)
            if len(state["console"]) > 3000:
                del state["console"][:1000]
            for ws in list(state["console_clients"]):
                try:
                    await ws.send_text(text)
                except Exception:  # noqa: BLE001
                    state["console_clients"].discard(ws)

    @app.websocket("/ws/console")
    async def ws_console(ws: WebSocket):
        if ws_user(ws) is None:
            await ws.close(code=4401)
            return
        await ws.accept()
        await ws.send_text("".join(state["console"][-800:]))
        state["console_clients"].add(ws)
        try:
            while True:
                await ws.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            state["console_clients"].discard(ws)

    # ---------------------------------------------------------------- runs (Tiled, via fastq.analysis)
    def tiled():
        if state["tiled"] is None:
            if not data_cfg.get("tiled_uri"):
                raise HTTPException(503, "no data.tiled_uri in lab.yaml")
            try:
                state["tiled"] = A.tiled_client(lab)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(503, f"data server {data_cfg['tiled_uri']}: {type(exc).__name__}: {exc}")
        return state["tiled"]

    def _find(ref: str):
        try:
            return A.find_run(tiled(), ref)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 - data server restarted: reconnect once
            state["tiled"] = None
            return A.find_run(tiled(), ref)

    def _recent(n: int = 300) -> list[dict]:
        """Summaries of the newest n runs, cached a few seconds (one paged request)."""
        cache = state.setdefault("recent", {"t": 0.0, "n": 0, "rows": []})
        if time.monotonic() - cache["t"] < 4.0 and cache["n"] >= n:
            return cache["rows"]
        try:
            items = list(tiled().items()[-1:-(n + 1):-1])
        except Exception:  # noqa: BLE001
            state["tiled"] = None
            items = list(tiled().items()[-1:-(n + 1):-1])
        rows = [A.summary(uid, node) for uid, node in items]
        cache.update(t=time.monotonic(), n=n, rows=rows)
        return rows

    @app.get("/api/runs")
    async def runs(limit: int = 50, offset: int = 0, q: str = "", plan: str = "", sample: str = "",
                   status: str = ""):
        """Newest first. q: words that must all appear in plan / comment / sample / kind / scan id."""
        rows = await asyncio.to_thread(_recent, 300)  # the newest 300 (a tiled page holds at most 300)
        words = [w.lower() for w in q.split()]

        def ok(r):
            text = " ".join(str(r.get(k) or "") for k in ("plan_name", "comment", "sample", "run_kind", "scan_id",
                                                           "uid"))
            return (all(w in text.lower() for w in words) and (not plan or r["plan_name"] == plan)
                    and (not sample or sample.lower() in str(r.get("sample") or "").lower())
                    and (not status or (r.get("exit_status") or "running") == status))
        hits = [r for r in rows if ok(r)]
        return hits[offset: offset + limit]

    def _run_detail(ref: str):
        uid, run = _find(ref)
        streams = {s: A.data_keys(run, s) for s in run}
        start = run.metadata.get("start") or {}
        defaults = A.default_plots(start, streams)
        return {**A.summary(uid, run), "start": start, "stop": run.metadata.get("stop"),
                "streams": {s: {k: {kk: v.get(kk) for kk in ("shape", "dtype", "units", "source", "precision")}
                                for k, v in dk.items()} for s, dk in streams.items()},
                "plots": _read_json(LAYOUTS).get(start.get("plan_name") or "") or defaults,
                "default_plots": defaults, "elog": E.run_index(lab).get(uid)}

    @app.get("/api/runs/{uid}")
    async def run_detail(uid: str):
        return await asyncio.to_thread(_run_detail, uid)

    def _plot(ref: str, spec: dict):
        _, run = _find(ref)
        stream = spec.get("stream", "primary")
        if stream not in run:
            return {"series": [], "n": 0, "kind": spec.get("kind", "line"), "missing": stream}
        cols = list(A.data_keys(run, stream))
        need = {spec.get("x"), spec.get("group"), spec.get("z"), *A._expand(spec.get("y") or [], cols)} - {None}
        df = A.table(run, stream, [c for c in need if c in cols or c in ("time", "seq_num")] or None)
        out = A.plot_data(df, spec)
        m = int(spec.get("max_points") or 4000)
        for s in out.get("series", []):
            step = max(1, math.ceil(len(s["x"]) / m))
            if step > 1:
                for k in ("x", "y", "err"):
                    if k in s:
                        s[k] = s[k][::step]
        return out

    @app.post("/api/runs/{uid}/plot")
    async def run_plot(uid: str, spec: dict):
        """Series for a plot spec (fastq.analysis.plot_data): x / y / group / average / combine / heatmap."""
        try:
            return await asyncio.to_thread(_plot, uid, spec)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"{type(exc).__name__}: {exc}")

    def _stored_frame(ref: str, key: str, index: int, stream: str = "primary"):
        _, run = _find(ref)
        arr = run[stream][key]
        n = arr.shape[0]
        i = max(0, min(n - 1, index if index >= 0 else n + index))
        img = np.asarray(arr[i], dtype=float)
        while img.ndim > 2:
            img = img[0]
        sy, sx = (max(1, math.ceil(d / 512)) for d in img.shape)  # per axis: spectrometer frames are 8 x 2048
        small = img[::sy, ::sx]
        return {"index": i, "count": n, "shape": list(img.shape), "step": sx, "step_y": sy, "width": int(small.shape[1]),
                "data": [_clean(v) for v in small.ravel().tolist()],
                "spectrum": [_clean(v) for v in img.sum(axis=0).tolist()]}

    @app.get("/api/runs/{uid}/frame")
    async def run_frame(uid: str, key: str, index: int = -1, stream: str = "primary"):
        """One stored camera frame (decimated to <= 512 px) + its spectrum."""
        return await asyncio.to_thread(_stored_frame, uid, key, index, stream)

    def _table(ref: str, stream: str, columns: str, max_points: int):
        _, run = _find(ref)
        want = [c for c in columns.split(",") if c] or None
        df = A.table(run, stream, want)
        step = max(1, math.ceil(len(df) / max(1, max_points)))
        out = {"n": int(len(df)), "step": step, "columns": {}}
        for col in df.columns:
            s = df[col].iloc[::step]
            if s.dtype.kind in "fiub":
                out["columns"][col] = [_clean(v) for v in s.astype(float).tolist()]
            elif s.dtype.kind == "O" and len(s) and isinstance(s.iloc[0], str):
                out["columns"][col] = s.tolist()
        return out

    @app.get("/api/runs/{uid}/table")
    async def run_table(uid: str, stream: str = "primary", columns: str = "", max_points: int = 5000):
        """Scalar columns of a stream (comma-separated `columns`, default all), decimated to max_points rows."""
        return await asyncio.to_thread(_table, uid, stream, columns, max_points)

    @app.get("/api/runs/{uid}/csv")
    async def run_csv(uid: str, stream: str = "primary"):
        df = await asyncio.to_thread(lambda: A.table(_find(uid)[1], stream))
        buf = io.StringIO()
        df.to_csv(buf, index=False)
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{uid[:8]}_{stream}.csv"'})

    @app.get("/api/runs/{uid}/hdf5")
    async def run_hdf5(uid: str, frames: bool = False):
        """All streams of a run (+ metadata, optionally the camera frames) in one HDF5 file."""
        import tempfile

        def make():
            u, node = _find(uid)
            run = A.RunView(u, node)
            path = Path(tempfile.mkdtemp()) / f"run{run.md.get('scan_id', '')}_{u[:8]}.h5"
            return A.export_hdf5(run, path, frames=frames)
        path = await asyncio.to_thread(make)
        return FileResponse(path, media_type="application/x-hdf5", filename=path.name)

    # ---------------------------------------------------------------- live run, plot layouts, presets
    LAYOUTS = data_root(lab) / "plot_layouts.json"     # {plan_name: [plot specs]} edited in the dashboard
    PRESETS = data_root(lab) / "presets.json"          # {plan_name: {preset: {kwargs, comment}}}
    from fastq.plan_registry import experiment_dirs  # shipped presets ("recipes") + private ones, read-only here

    def _read_json(path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    live_plots_cache: dict = {}

    def _live_plots(st: dict) -> list:
        """default_plots of the running run (its streams appear while it runs: re-read every 10 s)."""
        uid = st.get("uid")
        hit = live_plots_cache.get(uid)
        if hit and time.monotonic() - hit[0] < 10:
            return hit[1]
        try:
            d = _run_detail(uid)
            plots = d["default_plots"]
        except Exception:  # noqa: BLE001 - not in the data server yet
            plots = A.default_plots(st, {})
        live_plots_cache.clear()
        live_plots_cache[uid] = (time.monotonic(), plots)
        return plots

    @app.get("/api/live")
    async def live():
        """The current (or last) run from the RunEngine's live file: progress, position, plot layout."""
        st = read_live(lab) or {}
        if st.get("plan_name"):
            st["plots"] = _read_json(LAYOUTS).get(st["plan_name"]) or await asyncio.to_thread(_live_plots, st)
        if st.get("start_time"):
            st["elapsed_s"] = (st.get("stop_time") or time.time()) - st["start_time"]
            if st.get("num_points") and st.get("events") and not st.get("exit_status"):
                st["eta_s"] = st["elapsed_s"] / st["events"] * (st["num_points"] - st["events"])
        return st

    @app.get("/api/layouts/{plan}")
    async def layout_get(plan: str):
        return _read_json(LAYOUTS).get(plan)

    @app.put("/api/layouts/{plan}")
    async def layout_put(plan: str, body: list):
        data = _read_json(LAYOUTS)
        data[plan] = body
        _write_json(LAYOUTS, data)
        return {"ok": True}

    @app.delete("/api/layouts/{plan}")
    async def layout_delete(plan: str):
        data = _read_json(LAYOUTS)
        data.pop(plan, None)
        _write_json(LAYOUTS, data)
        return {"ok": True}

    @app.get("/api/presets")
    async def presets_get():
        """Shipped recipes (control/experiments/presets.yaml, "builtin": true) + the lab's own presets."""
        import yaml

        out: dict = {}
        for recipes in (d / "presets.yaml" for d in experiment_dirs()):
            try:
                for plan, ps in (yaml.safe_load(recipes.read_text(encoding="utf-8")) or {}).items():
                    out.setdefault(plan, {}).update({n: {**p, "builtin": True} for n, p in (ps or {}).items()})
            except FileNotFoundError:
                continue
            except (OSError, yaml.YAMLError) as exc:
                log.error("%s: %s", recipes, exc)
        for plan, ps in _read_json(PRESETS).items():
            out.setdefault(plan, {}).update(ps)
        return out

    @app.put("/api/presets/{plan}/{name}")
    async def preset_put(plan: str, name: str, body: dict):
        data = _read_json(PRESETS)
        data.setdefault(plan, {})[name] = {"kwargs": body.get("kwargs") or {}, "comment": body.get("comment", ""),
                                          "saved": time.time()}
        _write_json(PRESETS, data)
        return {"ok": True}

    @app.delete("/api/presets/{plan}/{name}")
    async def preset_delete(plan: str, name: str):
        data = _read_json(PRESETS)
        if name not in (data.get(plan) or {}):
            raise HTTPException(409, f"{name} is a shipped recipe (control/experiments/presets.yaml) - edit it there")
        (data.get(plan) or {}).pop(name, None)
        _write_json(PRESETS, data)
        return {"ok": True}

    @app.get("/api/pv/array")
    async def pv_array(name: str):
        """A whole waveform PV (the websocket decimates long arrays): camera live images."""
        def read():
            (pv,) = hub.ctx.get_pvs(name, timeout=2.0)
            pv.wait_for_connection(timeout=2.0)
            return [_clean(v) for v in np.asarray(pv.read(timeout=3.0).data, dtype=float).tolist()]
        try:
            return await asyncio.to_thread(read)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"{name}: {type(exc).__name__}: {exc}")

    def _plan_request(uid: str):
        _, run = _find(uid)
        start = run.metadata.get("start") or {}
        return start.get("plan_request"), start

    @app.get("/api/runs/{uid}/request")
    async def run_request(uid: str):
        """How a run's plan was called (md plan_request) - to fill the plan form - and whether the
        plan code changed since (then the exact version can be restored)."""
        try:
            req, start = await asyncio.to_thread(_plan_request, uid)
        except Exception:  # noqa: BLE001
            req, start = None, {}
        if not req:
            raise HTTPException(404, f"no parameters recorded for run {uid[:8]} ({start.get('plan_name', '?')})")
        from fastq.provenance import current_version

        from fastq.migrate import translate_request

        code = start.get("code") or {}
        name, kwargs, notes = translate_request(req["name"], req.get("kwargs") or {})  # runs before the renames
        return {"source": "run", **req, "name": name, "kwargs": kwargs, "renamed": notes, "code": code,
                "code_changed": bool(code.get("version")) and code["version"] != current_version()}

    @app.post("/api/runs/{uid}/restore_plan")
    async def run_restore_plan(uid: str):
        """Restore the exact plan code of a run from the code archive, then reload the plans."""
        from fastq.provenance import restore_plan

        busy = await _queue_busy()
        if busy:
            raise HTTPException(409, f"not while the queue is busy ({busy})")
        _, start = await asyncio.to_thread(_plan_request, uid)
        code = start.get("code") or {}
        if not (code.get("version") and str(code.get("plan_file", "")).endswith(".py")):
            raise HTTPException(404, "this run has no archived plan code")
        name, path = await asyncio.to_thread(restore_plan, lab, code["version"], code["plan_file"], code["function"])
        log.info("restored plan %s -> %s", name, path)
        await q_reload_plans()
        return {"plan": name, "file": str(path)}

    # ---------------------------------------------------------------- ELOG
    @app.post("/api/elog/entry")
    async def elog_entry(subject: str = Form(...), text: str = Form(""), logbook: str = Form(""),
                         run_uid: str = Form(""), category: str = Form("Comment"), author: str = Form(""),
                         files: list[UploadFile] = File(default=[]), request: Request = None):
        if not elog_cfg.get("url"):
            raise HTTPException(503, "no elog.url in lab.yaml")
        attachments = [(f.filename or f"attachment{i}.png", await f.read()) for i, f in enumerate(files)]
        user = getattr(request.state, "user", OPEN) if request else OPEN
        attrs = {"category": category, "author": author or (user["display"] if user is not OPEN else None)}
        if not run_uid:  # entries of a run inherit the run's sample / number / file
            attrs["sample"] = get_state(lab).get("sample")
        try:
            entry_id = E.post_entry(lab, logbook or None, subject, text, attrs, attachments, run_uid or None,
                                    kind="web")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"id": entry_id}

    def _elog_state():
        st = E.outbox_status(lab, limit=40)
        for items in st.values():
            for e in items:
                if e.get("msg_id"):
                    e["link"] = f"{elog_cfg.get('url', '').rstrip('/')}/{e['logbook']}/{e['msg_id']}"
        hb = E.elog_dir(lab) / "poster.json"
        st["poster"] = json.loads(hb.read_text(encoding="utf-8")) if hb.exists() else None
        st["current"] = E.current_experiment(lab)
        return st

    def _cached(key: str, func, ttl: float = 300.0):
        hit = state.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        value = func()
        state[key] = (time.time(), value)
        return value

    @app.get("/api/elog/logbooks")
    async def elog_logbooks(refresh: bool = False):
        """Logbooks on the ELOG server (front page), grouped as there."""
        if not elog_cfg.get("url"):
            return []
        if refresh:
            state.pop("elog_books", None)
        try:
            return await asyncio.to_thread(_cached, "elog_books", lambda: E.server_logbooks(lab))
        except Exception as exc:  # noqa: BLE001
            down = isinstance(exc, (OSError, ConnectionError)) or "Connection" in type(exc).__name__
            raise HTTPException(502, f"server {elog_cfg['url']} not reachable" if down
                                else f"ELOG server: {type(exc).__name__}: {exc}")

    @app.get("/api/elog/entries")
    async def elog_entries(logbook: str = "", n: int = 10, text: str = ""):
        """The newest entries of a logbook (default: the current notebook), as plain text."""
        if not elog_cfg.get("url"):
            return []
        try:
            return await asyncio.to_thread(E.read_entries, lab, logbook or None, min(n, 50), text)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"ELOG server: {type(exc).__name__}: {exc}")

    @app.get("/api/elog/options")
    async def elog_options(logbook: str):
        """Fixed choices of a logbook's attributes, e.g. {"Category": [...]}."""
        try:
            return await asyncio.to_thread(_cached, f"elog_opts:{logbook}", lambda: E.logbook_options(lab, logbook))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"ELOG server: {type(exc).__name__}: {exc}")

    @app.get("/api/elog/outbox")
    async def elog_outbox():
        return await asyncio.to_thread(_elog_state)

    @app.post("/api/elog/retry")
    async def elog_retry(body: dict):
        d = E.elog_dir(lab)
        src = d / "failed" / f"{Path(body['id']).name}.json"
        if not src.exists():
            raise HTTPException(404, body["id"])
        entry = json.loads(src.read_text(encoding="utf-8"))
        entry["attempts"] = 0
        src.write_text(json.dumps(entry), encoding="utf-8")
        src.replace(d / "pending" / src.name)
        att = d / "failed" / entry["id"]
        if att.exists():
            att.replace(d / "pending" / entry["id"])
        return {"ok": True}

    @app.get("/api/elog/runs")
    async def elog_runs(limit: int = 30):
        """Runs an entry can be attached to: run-start entries (sent or pending), newest first."""
        def scan():
            d = E.elog_dir(lab)
            out = []
            for sub in ("pending", "sent", "failed"):
                for f in sorted((d / sub).glob("*.json"), reverse=True)[:400]:
                    e = json.loads(f.read_text(encoding="utf-8"))
                    if e.get("kind") == "run_start":
                        out.append({"uid": e["run_uid"], "subject": e["subject"], "time": e["created"],
                                    "logbook": e["logbook"], "msg_id": e.get("msg_id")})
            return sorted(out, key=lambda r: -r["time"])[:limit]
        return await asyncio.to_thread(scan)

    # ---------------------------------------------------------------- system
    def _port(host: str, port: int) -> bool:
        try:
            socket.create_connection((host, port), 1.0).close()
            return True
        except OSError:
            return False

    def _storage() -> list:
        """Where the data of every run lives (on this control host)."""
        from fastq.session import data_root

        def size(path: Path):
            if path.is_file():
                return path.stat().st_size, 1
            total = n = 0
            for f in path.rglob("*") if path.exists() else []:
                if f.is_file():
                    total += f.stat().st_size
                    n += 1
            return total, n

        root, backup, tiled_dir = data_root(lab), backup_dir(lab), tiled_dir_of(lab)
        items = [("Raw documents (JSONL, one file per run - complete, independent of Tiled)", backup),
                 ("Tiled run tables (all event data)", tiled_dir / "tables.sqlite"),
                 ("Tiled catalog (run metadata, index)", tiled_dir / "catalog.db"),
                 ("Tiled array files", tiled_dir / "files"),
                 ("ELOG outbox", root / "elog")]
        out = []
        for label, path in items:
            b, n = size(path)
            out.append({"what": label, "path": str(path), "bytes": b, "files": n, "exists": path.exists()})
        try:
            import shutil as _sh

            du = _sh.disk_usage(root)
            out.append({"what": "Free disk space", "path": str(root.anchor or root), "bytes": du.free, "files": None,
                        "exists": True, "total": du.total})
        except OSError:
            pass
        return out

    def _storage_cached() -> list:
        if time.time() - state.get("storage_t", 0) > 60:
            state["storage"], state["storage_t"] = _storage(), time.time()
        return state["storage"]

    @app.get("/api/system")
    async def system():
        def check():
            tiled_u = urlparse(data_cfg.get("tiled_uri") or "http://127.0.0.1:8000")
            elog_u = urlparse(elog_cfg.get("url") or "")
            redis_port = int((lab.get("queue") or {}).get("redis_port", 6379))
            checks = [("Redis (queue store)", "127.0.0.1", redis_port),
                      ("Tiled data server", tiled_u.hostname, tiled_u.port or 80),
                      ("Queue server (control)", control_host, 60615),
                      ("Queue server (info)", control_host, 60625)]
            if elog_u.hostname:
                checks.append(("ELOG server", elog_u.hostname, elog_u.port or (443 if elog_u.scheme == "https" else 80)))
            with ThreadPoolExecutor(len(checks)) as pool:  # a service that is down costs its timeout once, not in a row
                ups = list(pool.map(lambda c: _port(c[1], c[2]), checks))
            services = [{"name": n, "addr": f"{h}:{p}", "up": up} for (n, h, p), up in zip(checks, ups)]
            hb = E.elog_dir(lab) / "poster.json" if elog_cfg.get("url") else None
            poster = json.loads(hb.read_text(encoding="utf-8")) if hb and hb.exists() else None
            if poster:
                poster["age_s"] = time.time() - poster["time"]
            from fastq import backup as B

            return {"services": services, "elog_poster": poster, "host": socket.gethostname(), "time": time.time(),
                    "storage": _storage_cached(), "backup": {**B.config(lab), "last": B.status(lab)}}
        return await asyncio.to_thread(check)

    @app.middleware("http")
    async def no_stale_js(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"  # revalidate: updates show up on reload
        return response

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lab", default=os.environ.get("FASTQ_LAB") or str(CONTROL / "lab.yaml"))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--read-only", action="store_true", help="no PV writes from the dashboards")
    ap.add_argument("--ssl-certfile")
    ap.add_argument("--ssl-keyfile")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("caproto").setLevel(logging.WARNING)
    import uvicorn

    lab = load_lab(a.lab)
    uvicorn.run(create_app(lab, a.read_only), host=a.host, port=a.port, log_level="warning",
                ssl_certfile=a.ssl_certfile, ssl_keyfile=a.ssl_keyfile)


if __name__ == "__main__":
    main()
