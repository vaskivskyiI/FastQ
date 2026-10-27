"""Bluesky session: lab config -> discovered devices -> RunEngine with data writers.

Used by the queue server (fastq.startup) and interactively:
    python -i -m fastq.startup          (IPython-like session with RE, devices, plans)

Lab config: control/lab.yaml, this PC's own (not in git; examples control/lab*.example.yaml), or the
file in FASTQ_LAB (e.g. control/lab_sim.yaml):
    directory: FASTQ:DIR:                  where devices are discovered
    roles: {temperature: itc2, ...}        which device plays which part
    data:  {tiled_uri, backup_dir}         API key: env FASTQ_TILED_API_KEY or <data>/tiled/api_key.txt
    metadata: {...}                        added to every run's start document
    elog: {url, user, ...}                 ELOG notebooks (fastq/elog.py), optional
Every key with a comment: control/lab.example.yaml.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import numpy as np
import yaml

log = logging.getLogger("fastq")
CONTROL_DIR = Path(__file__).resolve().parents[1]
_ROLES: dict = {}
_DEVICES: dict = {}


def load_lab(path: str | Path | None = None) -> dict:
    path = Path(path or os.environ.get("FASTQ_LAB") or CONTROL_DIR / "lab.yaml")
    lab = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    lab["_path"] = str(path)
    return lab


def backup_dir(lab: dict) -> Path:
    """Raw JSONL documents, one file per run (lab.yaml data.backup_dir, required: without it the
    data of a real lab must not end up in some default folder)."""
    value = (lab.get("data") or {}).get("backup_dir")
    if not value:
        raise ValueError(f"{lab.get('_path', 'lab file')}: data.backup_dir is required (e.g. D:/Data/fastq/jsonl)")
    backup = Path(value)
    return backup if backup.is_absolute() else CONTROL_DIR.parent / backup


def data_root(lab: dict) -> Path:
    """Folder that holds the JSONL backup, the Tiled catalog, the code archive and the ELOG outbox."""
    return backup_dir(lab).parent


def tiled_dir(lab: dict) -> Path:
    """Tiled catalog, its files and api_key.txt (start_control_host --data-dir overrides it)."""
    return Path(os.environ["FASTQ_TILED_DIR"]) if os.environ.get("FASTQ_TILED_DIR") else data_root(lab) / "tiled"


def tiled_api_key(lab: dict) -> str | None:
    key = os.environ.get("FASTQ_TILED_API_KEY")
    if key:
        return key
    f = tiled_dir(lab) / "api_key.txt"
    return f.read_text().strip() if f.exists() else None


def connect(lab: dict) -> dict:
    """Discover devices announced by the lab's directory IOC."""
    if lab.get("directory"):
        os.environ.setdefault("FASTQ_DIRECTORY", lab["directory"])
    from fastq.discovery import discover

    if (lab.get("derived") or {}).get("prefix"):
        os.environ.setdefault("FASTQ_DERIVED_PREFIX", lab["derived"]["prefix"])
    try:
        devices = discover()
    except ConnectionError as exc:  # no IOC host up yet: start empty, "Reload plans" picks the devices up later
        log.warning("%s - starting without devices", exc)
        devices = {}
    _DEVICES.clear()
    _DEVICES.update(devices)
    _ROLES.clear()
    unavailable = []
    for role, name in (lab.get("roles") or {}).items():
        if name in devices:
            _ROLES[role] = devices[name]
            from fastq.roles import check

            for problem in check(role, devices[name]):
                log.warning(problem)
        else:
            unavailable.append(f"{role} ({name})")
    if unavailable:
        log.warning("roles without a running device: %s", ", ".join(unavailable))
    return devices


def role(name: str):
    """The device currently playing `name` (temperature, lockin, laser, keithley)."""
    if name not in _ROLES:
        raise LookupError(f"no device for role {name!r} (roles: {sorted(_ROLES)}) - check lab.yaml / IOCs")
    return _ROLES[name]


def resolve(value=None, role_name: str | None = None, required: bool = True):
    """THE way plans get a device: an object is returned as is; a name ("delay1"), component path
    ("itc1.temp") or "@role" is looked up in the session; None means the lab role `role_name`
    (None again when the lab has none and required=False). Names inside lists/dicts reach plans as
    text (the queue server converts only top-level names) - so every plan input goes through here."""
    if value is not None and not isinstance(value, str):
        return value
    if isinstance(value, str):
        if ":" in value:
            raise ValueError(f"{value!r} looks like a PV prefix - use the device name (e.g. mfli1), see the dashboard")
        return device_by_name(value)
    if role_name is None:
        return None
    try:
        return role(role_name)
    except LookupError:
        if required:
            raise
        return None


def device_by_name(name: str):
    """A session device by name ("delay1"), component path ("itc1.temp") or role ("@temperature")."""
    if name.startswith("@"):
        return role(name[1:])
    head, _, rest = name.partition(".")
    if head not in _DEVICES:
        raise LookupError(f"no device {head!r} in this session (devices: {sorted(_DEVICES)})")
    obj = _DEVICES[head]
    for part in rest.split(".") if rest else []:
        obj = getattr(obj, part)
    return obj


def jsonl_name(start_doc: dict) -> str:
    """File name of a run's raw JSONL document file (also quoted in ELOG as "File")."""
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(start_doc["time"]))
    return f"{stamp}_{start_doc.get('plan_name', 'run')}_{start_doc['uid'][:8]}.jsonl"


class JSONLWriter:
    """Local copy of every run as JSON lines (one document per line,
    suitcase-jsonl layout): <backup_dir>/<date>_<plan>_<uid8>.jsonl.
    Independent of the data server - nothing is lost if it is down."""

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._file = None

    @staticmethod
    def _default(obj):
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, np.generic):
            return obj.item()
        return str(obj)

    def __call__(self, name: str, doc: dict) -> None:
        if name == "start":
            path = self.dir / jsonl_name(doc)
            self._file = open(path, "w", encoding="utf-8")
        if self._file is not None:
            self._file.write(json.dumps([name, doc], default=self._default) + "\n")
            self._file.flush()
        if name == "stop" and self._file is not None:
            self._file.close()
            self._file = None


def live_tiled_writer(client, flush_s: float = 2.0):
    """TiledWriter that appends each stream's rows to Tiled at least every
    flush_s seconds (stock TiledWriter: only every batch_size events, i.e.
    nothing visible until the run ends), running in its own thread so the
    HTTP writes never delay the plan. Built on bluesky 1.15 TiledWriter internals."""
    from bluesky.callbacks.buffer import BufferingWrapper
    from bluesky.callbacks.tiled_writer import TiledWriter, _RunWriter

    class TimedRunWriter(_RunWriter):
        def __init__(self, client):
            super().__init__(client, batch_size=1_000_000)
            self._last_flush = time.monotonic()

        def event(self, doc):
            super().event(doc)
            if time.monotonic() - self._last_flush >= flush_s:
                self._last_flush = time.monotonic()
                for name, cache in self._internal_data_cache.items():
                    if cache:
                        self._write_internal_data(cache, desc_node=self._desc_nodes[name])
                        cache.clear()

    class LiveTiledWriter(TiledWriter):
        def _factory(self, name, doc):
            cb = run_writer = TimedRunWriter(self.client)
            if self._normalizer:
                cb = self._normalizer(patches=self.patches, spec_to_mimetype=self.spec_to_mimetype)
                cb.subscribe(run_writer)
            return [cb], []

    return BufferingWrapper(LiveTiledWriter(client))


def _reachable(uri: str, timeout: float = 2.0) -> bool:
    import socket
    from urllib.parse import urlparse

    u = urlparse(uri)
    try:
        socket.create_connection((u.hostname, u.port or 80), timeout).close()
        return True
    except OSError:
        return False


def make_run_engine(lab: dict, table: bool = True):
    from bluesky import RunEngine

    from fastq.experiment import experiment_preprocessor

    from fastq.provenance import persistent_scan_id_source

    RE = RunEngine({}, scan_id_source=persistent_scan_id_source(lab))  # run numbers never reset
    RE.md.update(lab.get("metadata") or {})
    RE.preprocessors.append(experiment_preprocessor(lab))  # current sample / notebook (dashboard) per run
    from fastq.detectors import remember_run

    RE.subscribe(remember_run)  # camera frame files are named after the run
    from fastq.live import LiveStatus

    RE.subscribe(LiveStatus(lab))  # <data>/live.json: progress for the dashboards
    data = lab.get("data") or {}
    if data.get("backup_dir"):
        RE.subscribe(JSONLWriter(backup_dir(lab)))
    if data.get("tiled_uri") and _reachable(data["tiled_uri"]):
        try:
            from tiled.client import from_uri

            client = from_uri(data["tiled_uri"], api_key=tiled_api_key(lab))
            RE.subscribe(live_tiled_writer(client, flush_s=float(data.get("flush_s", 2.0))))
            log.info("runs are written to %s", data["tiled_uri"])
        except Exception as exc:
            log.error("data server %s not available (%s) - runs only in the JSONL backup", data["tiled_uri"], exc)
    elif data.get("tiled_uri"):
        log.error("data server %s not reachable - runs only in the JSONL backup", data["tiled_uri"])
    if (lab.get("elog") or {}).get("url"):
        from fastq.elog import ElogRunCallback

        RE.subscribe(ElogRunCallback(lab))  # only queues entries; the ELOG service posts them
    if table:
        from bluesky.callbacks.best_effort import BestEffortCallback

        bec = BestEffortCallback()
        bec.disable_plots()
        RE.subscribe(bec)
    return RE
