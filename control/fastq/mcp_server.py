"""MCP server: the lab for AI assistants (Claude Desktop / Claude Code / any MCP client).

    python -m fastq.mcp_server                       (stdio; run from control/)
    python -m fastq.mcp_server --http --port 8089    (streamable HTTP for remote clients)

It is a client of the dashboard web server (control/web/server.py), so an
assistant sees and may do exactly what a person in the browser may: the same
login, PV writes only for dashboard controls and never while a plan runs, the
same queue. Nothing here talks to instruments directly.

Environment:
  FASTQ_URL            dashboard URL (default http://127.0.0.1:8088)
  FASTQ_AI_USER / FASTQ_AI_PASSWORD     the dashboard account the assistant acts as (fastq.users;
                       give it its own account, e.g. "ai" with role viewer or operator - the audit log
                       then shows what the assistant did)
  FASTQ_AI_ACCESS      read (default) | queue (+ add/remove queue items, ELOG notes)
                       | control (+ start/stop the queue, device controls)

Claude Desktop / Claude Code config example:
  {"mcpServers": {"fastq": {"command": "D:/git/FastQ/.venv/Scripts/python.exe",
     "args": ["-m", "fastq.mcp_server"], "cwd": "D:/git/FastQ/control",
     "env": {"FASTQ_URL": "http://<control-host>:8088", "FASTQ_AI_ACCESS": "queue"}}}}
"""
from __future__ import annotations

import argparse
import os

import httpx

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # 1.x
    from mcp.server.fastmcp import FastMCP

LEVELS = ("read", "queue", "control")
URL = os.environ.get("FASTQ_URL", "http://127.0.0.1:8088").rstrip("/")
ACCESS = os.environ.get("FASTQ_AI_ACCESS", "read")
if ACCESS not in LEVELS:
    raise SystemExit(f"FASTQ_AI_ACCESS must be one of {LEVELS}")

mcp = FastMCP("fastq", instructions=(
    "FastQ lab control: instruments are EPICS IOCs on several PCs, experiments are Bluesky plans run by a queue "
    "server, data is in Tiled, the logbook is ELOG. Start with lab_overview(). Plans take devices by name and a "
    "free-text `comment` (the run description). Prefer queueing plans over touching devices; never change "
    "laser, magnet, heater or high-voltage settings unless the user explicitly asked for that exact change. "
    "To propose the next measurements: read the notebook (elog_read), recent runs (list_runs, run_details, "
    "run_plot), how values behaved (value_history, alarms) and the recipes (list_recipes); explain the proposal "
    "and queue it (queue_recipe / queue_add) only after the user agreed. wait_for / wait_until / sequence "
    "build unattended nights."))


_session: dict = {}


def _client() -> httpx.Client:
    """A client with the dashboard session of FASTQ_AI_USER (logged in once, again when it expired)."""
    c = httpx.Client(base_url=URL, timeout=30.0, cookies=_session.get("cookies"))
    if os.environ.get("FASTQ_AI_USER") and not _session.get("cookies"):
        r = c.post("/api/login", json={"user": os.environ["FASTQ_AI_USER"],
                                       "password": os.environ.get("FASTQ_AI_PASSWORD", "")})
        r.raise_for_status()
        _session["cookies"] = dict(c.cookies)
    return c


def _retry_login(r: httpx.Response) -> bool:
    if r.status_code == 401 and _session.get("cookies"):
        _session.clear()  # session expired: log in again
        return True
    return False


def _get(path: str, **params):
    for _ in range(2):
        with _client() as c:
            r = c.get(path, params={k: v for k, v in params.items() if v is not None})
        if not _retry_login(r):
            break
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict | None = None, **kw):
    for _ in range(2):
        with _client() as c:
            r = c.post(path, json=body, **kw)
        if not _retry_login(r):
            break
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code}: {r.text[:500]}")
    return r.json()


def _need(level: str) -> None:
    if LEVELS.index(ACCESS) < LEVELS.index(level):
        raise PermissionError(f"this assistant has '{ACCESS}' access; '{level}' is needed "
                              f"(set FASTQ_AI_ACCESS={level} when starting the MCP server)")


# ------------------------------------------------------------------ reading
@mcp.tool()
def lab_overview() -> dict:
    """The setup: lab config (roles = which device plays which part), current sample and ELOG
    notebook, every device (name, type, IOC host, online), queue state and the access level."""
    lab = _get("/api/lab")
    devs = _get("/api/devices")
    status = {}
    try:
        status = _get("/api/queue/status")
    except Exception as exc:  # noqa: BLE001
        status = {"error": str(exc)}
    return {"lab": lab, "experiment": _get("/api/experiment"), "devices": devs.get("devices"),
            "queue": {k: status.get(k) for k in ("manager_state", "re_state", "items_in_queue",
                                                  "running_item_uid", "worker_environment_exists")},
            "ai_access": ACCESS}


@mcp.tool()
def describe_device(name: str) -> dict:
    """What a device is and can do: description, every parameter (units, limits, docs, writable,
    positioner = usable as a scan axis, safety), actions, and for derived values their formula."""
    try:
        return _get(f"/api/schema/{name}")
    except httpx.HTTPStatusError:
        profiles = _get("/api/profiles")
        dev = next((d for d in _get("/api/devices")["devices"] if d["name"] == name), None)
        return {"name": name, "profile": profiles.get(dev["type"]) if dev else None}


@mcp.tool()
def read_device(name: str) -> dict:
    """Current values of a device's fields (live from the IOC; long arrays decimated)."""
    return _get(f"/api/devices/{name}/values")


@mcp.tool()
def list_plans(name_contains: str = "") -> dict:
    """Plans that can be queued: name -> docstring and parameters (with defaults / annotations)."""
    allowed = _get("/api/queue/plans").get("plans_allowed", {})
    out = {}
    for name, p in sorted(allowed.items()):
        if name_contains and name_contains.lower() not in name.lower():
            continue
        out[name] = {"description": (p.get("description") or "").strip(),
                     "parameters": [{k: v for k, v in par.items() if k in ("name", "default", "annotation",
                                                                         "description", "kind")}
                                    for par in p.get("parameters", [])]}
    return out


@mcp.tool()
def queue_state() -> dict:
    """Queue server state, the queued items, the running item and the recent history."""
    return {"status": _get("/api/queue/status"), "items": _get("/api/queue/items")}


@mcp.tool()
def list_runs(limit: int = 20) -> list:
    """Recent runs (newest first): uid, scan_id, plan, sample, comment, start time, exit status."""
    return _get("/api/runs", limit=limit)


@mcp.tool()
def run_details(uid: str) -> dict:
    """One run: full start/stop metadata (incl. the exact plan call), streams and their columns,
    its ELOG entry. uid may be a prefix, or 'latest'."""
    return _get(f"/api/runs/{uid}")


@mcp.tool()
def run_data(uid: str, stream: str = "primary", columns: list[str] | None = None, max_points: int = 500) -> dict:
    """Data of one stream of a run as columns (decimated to max_points rows). Choose columns from
    run_details; arrays (spectra) are long - ask only for the columns you need."""
    return _get(f"/api/runs/{uid}/table", stream=stream, columns=",".join(columns or []), max_points=max_points)


@mcp.tool()
def run_plot(uid: str, x: str, y: list[str], group: str = "", average: bool = False, combine: str = "",
             stream: str = "primary") -> dict:
    """Processed curves of a run, as the dashboard plots them: y vs x, one curve per value of `group`
    (e.g. pump_shutter), `average` = mean +- s.e.m. over repeats, `combine` = diff | ratio | rel_diff
    between the two group values (pump-probe signal). y may use globs (cammain_over_camref_*)."""
    spec = {"x": x, "y": y, "stream": stream, "average": average, "max_points": 1000}
    if group:
        spec["group"] = group
    if combine:
        spec["combine"] = combine
    return _post(f"/api/runs/{uid}/plot", spec)


@mcp.tool()
def live_run() -> dict:
    """The run in progress (or the last one): plan, comment, points done / total, ETA, current position."""
    return _get("/api/live")


@mcp.tool()
def elog_runs(limit: int = 20) -> list | dict:
    """ELOG entries the DAQ wrote for recent runs (logbook, entry id, run)."""
    return _get("/api/elog/runs", limit=limit)


@mcp.tool()
def elog_read(logbook: str = "", n: int = 10, text: str = "") -> list | dict:
    """The newest ELOG entries (default: the current experiment notebook) as plain text, optionally
    only those containing `text` - what the scientists wrote: samples, problems, plans for the next runs."""
    return _get("/api/elog/entries", logbook=logbook or None, n=n, text=text or None)


@mcp.tool()
def list_recipes() -> dict:
    """Recipes = plans with fixed parameters (control/experiments/presets.yaml + the lab's saved presets):
    {plan: {name: {kwargs, comment, setup}}}. Queue one with queue_recipe, adjusting what the user asked."""
    return _get("/api/presets")


@mcp.tool()
def value_history(device_attr: str, hours: float = 24.0, points: int = 300) -> dict:
    """Recorded values of a device value ("itc1.temp", "vac.pressure1", "derived.RSample") over the last
    hours (min/max envelope for long spans): look at drifts, stability, what happened overnight."""
    dev, _, attr = device_attr.partition(".")
    schema = _get(f"/api/schema/{dev}")
    p = next((p for p in schema.get("params", []) if p["attr"] == attr), None)
    if p is None:
        raise ValueError(f"{dev} has no value {attr}; its values: {[x['attr'] for x in schema.get('params', [])]}")
    prefix = next(d["prefix"] for d in _get("/api/devices")["devices"] if d["name"] == dev)
    import time as _t

    end = _t.time()
    return _get("/api/history", pv=prefix + p["name"] + ("_RBV" if p.get("rbv") else ""), start=end - hours * 3600,
                end=end, points=points)


@mcp.tool()
def alarms() -> dict:
    """Alarm rules on device values, which are active now and the recent alarm events."""
    return _get("/api/alarms", since=0)


@mcp.tool()
def derived_values() -> dict:
    """How this experiment derives quantities (e.g. RSample from the lock-in X and amplitude): formulas,
    inputs, constants. Every run records the definitions valid at its start (run md "derived")."""
    return _get("/api/derived")


# ------------------------------------------------------------------ acting
@mcp.tool()
def queue_recipe(plan: str, recipe: str, changes: dict | None = None, comment: str = "",
                 position: str = "back") -> dict:
    """Queue a recipe (see list_recipes) with some parameters changed, e.g. queue_recipe("rt_sweep",
    "Thermal cycle 4 -> 300 -> 4 K", {"current_a": 5e-7}). Needs 'queue' access."""
    _need("queue")
    r = (_get("/api/presets").get(plan) or {}).get(recipe)
    if r is None:
        raise ValueError(f"no recipe {recipe!r} for {plan}")
    kw = {**(r.get("kwargs") or {}), **(changes or {})}
    kw["comment"] = comment or r.get("comment") or recipe
    return _post("/api/queue/add", {"name": plan, "kwargs": kw, "pos": position})
@mcp.tool()
def queue_add(plan: str, kwargs: dict, comment: str = "", position: str = "back") -> dict:
    """Add a plan to the queue (does NOT start it). kwargs: plan parameters, devices by name.
    comment: what this run is (goes to the run metadata and the ELOG). Needs 'queue' access."""
    _need("queue")
    kw = dict(kwargs)
    if comment:
        kw["comment"] = comment
    return _post("/api/queue/add", {"name": plan, "kwargs": kw, "pos": position})


@mcp.tool()
def queue_remove(item_uid: str) -> dict:
    """Remove a queued (not running) item. Needs 'queue' access."""
    _need("queue")
    return _post("/api/queue/remove", {"uid": item_uid})


@mcp.tool()
def queue_control(action: str) -> dict:
    """start | stop (the queue, after the current plan) | pause | resume | end_run (end the running or
    paused run, success) | abort (end it as aborted). Needs 'control' access - confirm with the user first."""
    _need("control")
    if action in ("end_run", "re_stop", "abort"):
        return _post(f"/api/run/{'abort' if action == 'abort' else 'stop'}", {})
    if action not in ("start", "stop", "pause", "resume"):
        raise ValueError("unknown action")
    return _post(f"/api/queue/{action}", {})


@mcp.tool()
def set_device(name: str, field: str, value: float | int | str) -> dict:
    """Write one dashboard control of a device (field = PV suffix, e.g. 'Setpoint' or an action
    name). Refused while a plan runs; controls the IOC marks protected (need the lab PIN) are
    never writable from here. Needs 'control' access - confirm with the user first."""
    _need("control")
    dev = next((d for d in _get("/api/devices")["devices"] if d["name"] == name), None)
    if dev is None:
        raise ValueError(f"no device {name}")
    return _post("/api/pv/put", {"pv": dev["prefix"] + field, "value": value, "who": "mcp"})


@mcp.tool()
def elog_note(subject: str, text: str, run_uid: str = "", category: str = "Comment") -> dict:
    """Post a note to the current ELOG notebook (as a reply in a run's thread if run_uid given).
    Needs 'queue' access."""
    _need("queue")
    with _client() as c:
        r = c.post("/api/elog/entry", data={"subject": subject, "text": text, "run_uid": run_uid,
                                            "category": category, "author": "AI assistant"})
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code}: {r.text[:500]}")
        return r.json()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--http", action="store_true", help="streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8089)
    a = ap.parse_args()
    if not a.http:
        mcp.run()
        return
    try:  # SDK 2.x
        mcp.run(transport="streamable-http", host=a.host, port=a.port)
    except TypeError:  # SDK 1.x
        mcp.settings.host, mcp.settings.port = a.host, a.port
        mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
