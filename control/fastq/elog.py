"""ELOG integration (PSI ELOG, https://elog.psi.ch/elog/, client: py_elog).

Everything that should go to ELOG is first written to a durable OUTBOX
(spool folder on the control host); one poster process (`python -m
fastq.elog`, started by start_control_host.py) sends the entries in order and
retries while ELOG is unreachable - data taking never waits for ELOG.

Entries:
  * run start      new thread in the experiment logbook (Bluesky callback ElogRunCallback)
  * run stop       reply to the run's start entry (status, duration, streams)
  * comments /     reply to the run's start entry, or a standalone entry - posted from the
    custom entries web GUI (text + screenshots / files) or from Python: post_entry(...)
  * device events  device logbooks (connection lost/restored, IOC restarts, LastError of every
                   IOC, laser errors, Keithley errors) - DeviceEventWatcher, re-scans every minute

Experiment logbook: each experiment has its own notebook. The CURRENT one is
fastq.experiment state (<data>/experiment.json, set in the dashboard top bar); a plan can
override it per run with md={"elog_logbook": "..."}.

Configuration: lab.yaml `elog:` section (control/lab.example.yaml); password in env
FASTQ_ELOG_PASSWORD or elog.password_file.
"""
from __future__ import annotations

import html
import json
import logging
import re
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger("fastq.elog")


# ------------------------------------------------------------------ config / paths
def elog_config(lab: dict) -> dict:
    return dict(lab.get("elog") or {})


def elog_dir(lab: dict) -> Path:
    from fastq.session import data_root

    d = data_root(lab) / "elog"
    for sub in ("pending", "sent", "failed"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def elog_password(lab: dict) -> str | None:
    """env FASTQ_ELOG_PASSWORD, else the file elog.password_file (default <data>/elog/password.txt)."""
    if os.environ.get("FASTQ_ELOG_PASSWORD"):
        return os.environ["FASTQ_ELOG_PASSWORD"]
    f = Path(elog_config(lab).get("password_file") or elog_dir(lab) / "password.txt")
    return f.read_text(encoding="utf-8").strip() if f.exists() else None


def current_experiment(lab: dict) -> str | None:
    """The current experiment notebook (fastq.experiment state, set on the dashboard)."""
    from fastq.experiment import get_state

    return get_state(lab).get("notebook")


# logical entry field -> attribute name in the ELOG logbooks (lab.yaml elog.attributes overrides;
# fields whose attribute a logbook does not have are left out by removing them there)
DEFAULT_ATTRIBUTES = {"run": "RunN", "sample": "Sample", "file": "File", "category": "Category", "subject": "Subject",
                      "author": "Author", "status": "Status", "plan": "Plan"}
RUN_FIELDS = ("run", "sample", "file", "plan")   # inherited by every entry of a run's thread


def attribute_names(lab: dict) -> dict:
    return dict(elog_config(lab).get("attributes") or DEFAULT_ATTRIBUTES)


def run_index(lab: dict) -> dict:
    """{run uid: {"logbook", "msg_id", "attrs"}} of posted run-start entries (written by the poster only)."""
    f = elog_dir(lab) / "runs.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def auto_category(lab: dict, kind: str) -> str:
    """Category of automatic entries (must be one of the logbook's Category options)."""
    return ({"run": "Routine entry", "device_event": "Other"} | (elog_config(lab).get("auto_categories") or {}))[kind]


# ------------------------------------------------------------------ what the ELOG server offers
def _session_cookies(lab: dict) -> dict:
    from passlib.hash import sha256_crypt

    cfg, pw = elog_config(lab), elog_password(lab)
    cookies = {"unm": cfg["user"]} if cfg.get("user") else {}
    if pw:
        cookies["upwd"] = sha256_crypt.using(salt="", rounds=5000).hash(pw)[4:]   # as py_elog does
    return cookies


def _get(lab: dict, path: str) -> str:
    import requests
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    r = requests.get(elog_config(lab)["url"].rstrip("/") + "/" + path, cookies=_session_cookies(lab),
                     verify=False, timeout=15)
    r.raise_for_status()
    if 'type="password"' in r.text or "type=password" in r.text:
        raise PermissionError("ELOG login failed (user / password)")
    return r.text


def server_logbooks(lab: dict) -> list[dict]:
    """Logbooks listed on the ELOG front page: [{name, group, comment, entries, last}]."""
    from urllib.parse import unquote_plus

    page = re.sub(r"\s+", " ", _get(lab, ""))
    books, group = [], ""
    for row in re.findall(r"<tr>(.*?)</tr>", page):
        g = re.search(r'class="selgroup"[^>]*>(.*?)</td>', row)
        if g:
            group = html.unescape(re.sub("<.*?>", "", g.group(1))).replace("\xa0", " ").strip()
            continue
        m = re.search(r'class="sellogbook"><a href="([^"]+)">', row)
        if not m:
            continue
        comment = re.search(r'class="selcomment">(.*?)</span>', row)
        cells = re.findall(r'class="selentries">(.*?)</td>', row)
        books.append({"name": unquote_plus(m.group(1)), "group": group,
                      "comment": html.unescape(re.sub("<.*?>", "", comment.group(1))).strip() if comment else "",
                      "entries": cells[0].strip() if cells else "", "last": html.unescape(cells[1]).strip() if len(cells) > 1 else ""})
    cfg = elog_config(lab)  # a setup offers only its own notebooks (4dmag: group "4DMAG Experiments", 4dXXX)
    groups, pattern = cfg.get("notebook_groups"), cfg.get("notebook_pattern")
    if groups:
        books = [b for b in books if b["group"] in groups]
    if pattern:
        books = [b for b in books if re.search(pattern, b["name"])]
    return books


def read_entries(lab: dict, logbook: str | None = None, n: int = 10, text: str = "") -> list[dict]:
    """The newest n entries of a logbook (default: the current experiment notebook): id, date,
    attributes, plain text (HTML stripped) - for the AI tools and searches. text: only entries
    containing it (case-insensitive; searches the newest 10 n)."""
    import elog as pyelog
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    cfg = elog_config(lab)
    name = logbook or current_experiment(lab)
    book = pyelog.open(cfg["url"], logbook=name, user=cfg.get("user"), password=elog_password(lab))
    out = []
    for msg_id in sorted(book.get_message_ids(timeout=30), reverse=True)[: n * (10 if text else 1)]:
        message, attrs, _ = book.read(msg_id, timeout=30)
        plain = html.unescape(re.sub(r"<[^>]+>", " ", message or ""))
        plain = re.sub(r"\s+", " ", plain).strip()
        if text and text.lower() not in (plain + json.dumps(attrs)).lower():
            continue
        out.append({"logbook": name, "id": int(msg_id), "attributes": attrs, "text": plain[:4000]})
        if len(out) >= n:
            break
    return out


def logbook_options(lab: dict, logbook: str) -> dict:
    """{attribute: [allowed values]} for the logbook's fixed-choice attributes (from its entry form)."""
    from urllib.parse import quote_plus

    page = _get(lab, quote_plus(logbook) + "/?cmd=New")
    out = {}
    for name, body in re.findall(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', page, flags=re.S):
        opts = [html.unescape(o) for o in re.findall(r'<option[^>]*value="([^"]*)"', body) if o]
        if opts:
            out[name] = opts
    for name in set(re.findall(r'type="?radio"?[^>]*name="([^"]+)"', page)):  # radio-button attributes
        out[name] = [html.unescape(v) for v in re.findall(rf'name="{re.escape(name)}"[^>]*value="([^"]*)"', page)]
    return out


def _ascii_html(text: str) -> str:
    """HTML text with every non-ASCII character as an entity - survives any server charset."""
    return text.encode("ascii", "xmlcharrefreplace").decode("ascii")


def _utf8_wire(value: str) -> str:
    """py_elog encodes attribute values as latin-1; the server reads UTF-8 -> hand py_elog
    the UTF-8 bytes as latin-1 characters so they arrive as UTF-8 (µ, °, Ω in sample names)."""
    return value.encode("utf-8").decode("latin-1")


# ------------------------------------------------------------------ outbox
def post_entry(lab: dict, logbook: str | None, subject: str, text: str, attributes: dict | None = None,
               attachments: list | None = None, run_uid: str | None = None, kind: str = "comment") -> str:
    """Queue an entry. attributes: logical fields {category, sample, run, file, author}
    (mapped to the logbook's attribute names when posted). attachments: paths or
    (filename, bytes). run_uid: post as a reply to that run's start entry, inheriting
    its run / sample / file. Returns the outbox id."""
    logbook = logbook or current_experiment(lab)
    if not logbook:
        raise ValueError("no logbook given and no current experiment set")
    entry_id = f"{time.strftime('%Y%m%d-%H%M%S')}_{uuid.uuid4().hex[:8]}"
    pending = elog_dir(lab) / "pending"
    att_dir = pending / entry_id
    files = []
    for i, att in enumerate(attachments or []):
        att_dir.mkdir(exist_ok=True)
        if isinstance(att, (tuple, list)):
            name, data = att
            target = att_dir / f"{i:02d}_{Path(name).name}"
            target.write_bytes(data)
        else:
            target = att_dir / f"{i:02d}_{Path(att).name}"
            shutil.copy2(att, target)
        files.append(target.name)
    entry = {"id": entry_id, "created": time.time(), "logbook": logbook, "subject": subject, "text": text,
             "attributes": attributes or {}, "attachments": files, "run_uid": run_uid, "kind": kind,
             "attempts": 0, "error": None}
    tmp = pending / f"{entry_id}.json.tmp"
    tmp.write_text(json.dumps(entry, default=str), encoding="utf-8")
    tmp.rename(pending / f"{entry_id}.json")  # atomic: the poster never sees half-written entries
    return entry_id


def outbox_status(lab: dict, limit: int = 30) -> dict:
    d = elog_dir(lab)
    out = {}
    for sub in ("pending", "sent", "failed"):
        files = sorted((d / sub).glob("*.json"), reverse=True)[:limit]
        out[sub] = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    return out


class Poster:
    """Single consumer of the outbox: posts entries oldest first, replies
    wait until their run's start entry has an ELOG id."""

    MAX_ATTEMPTS = 50
    MARKER = "fastq-entry:"

    def __init__(self, lab: dict) -> None:
        self.lab = lab
        self.cfg = elog_config(lab)
        self.dir = elog_dir(lab)
        self._books: dict = {}

    def _logbook(self, name: str):
        if name not in self._books:
            import elog as pyelog
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # py_elog posts with verify=False
            self._books[name] = pyelog.open(self.cfg["url"], logbook=name, user=self.cfg.get("user"),
                                            password=elog_password(self.lab))
        return self._books[name]

    def _save_run_index(self, uid: str, logbook: str, msg_id: int, attrs: dict) -> None:
        idx = run_index(self.lab)
        idx[uid] = {"logbook": logbook, "msg_id": msg_id,
                    "attrs": {k: v for k, v in attrs.items() if k in RUN_FIELDS}}
        tmp = self.dir / "runs.json.tmp"
        tmp.write_text(json.dumps(idx, indent=1), encoding="utf-8")
        tmp.replace(self.dir / "runs.json")

    def post_one(self, path: Path) -> bool:
        entry = json.loads(path.read_text(encoding="utf-8"))
        reply_to = None
        if entry.get("run_uid") and entry["kind"] != "run_start":
            run = run_index(self.lab).get(entry["run_uid"])
            if run is None:
                if time.time() - entry["created"] < 600:
                    return False  # the run's start entry is not posted yet - keep waiting
            else:
                reply_to = run["msg_id"]
                entry["logbook"] = run["logbook"]
        fields = dict(entry["attributes"])
        if entry.get("run_uid"):
            for k, v in ((run_index(self.lab).get(entry["run_uid"]) or {}).get("attrs") or {}).items():
                fields.setdefault(k, v)
        fields.setdefault("author", self.cfg.get("author", "FastQ DAQ"))
        fields["subject"] = entry["subject"]
        names = attribute_names(self.lab)
        attributes = {names[k]: _utf8_wire(str(v)) for k, v in fields.items() if k in names and v not in (None, "")}
        att_dir = path.parent / entry["id"]
        attachments = [str(att_dir / f) for f in entry["attachments"]]
        # ELOG transfers latin-1 only: send HTML so any character (Ω, °, ...) survives as an entity;
        # the hidden marker identifies the entry if the server's reply does not (see _find_posted)
        text = _ascii_html(f'<pre style="white-space:pre-wrap">{html.escape(entry["text"])}</pre>'
                           f'<!-- {self.MARKER}{entry["id"]} -->')
        try:
            book = self._logbook(entry["logbook"])
            msg_id = self._find_posted(book, entry["id"]) if entry["attempts"] else None  # never post twice
            if msg_id is None:
                try:
                    if reply_to:
                        msg_id = book.post(text, msg_id=reply_to, reply=True, attributes=attributes,
                                           attachments=attachments or None, encoding="HTML", timeout=30)
                    else:
                        msg_id = book.post(text, attributes=attributes, attachments=attachments or None,
                                           encoding="HTML", timeout=30)
                except Exception as exc:  # outcome unknown (no id in the reply, timeout, ...) - did it arrive?
                    if "Rejected" in type(exc).__name__ or "Authentication" in type(exc).__name__:
                        raise
                    msg_id = self._find_posted(book, entry["id"])
                    if msg_id is None:
                        raise
        except Exception as exc:
            entry["attempts"] += 1
            entry["error"] = f"{type(exc).__name__}: {exc}"[:500]
            target = "failed" if entry["attempts"] >= self.MAX_ATTEMPTS or "Rejected" in entry["error"] else None
            path.write_text(json.dumps(entry), encoding="utf-8")
            if target:
                self._move(path, att_dir, target)
            log.warning("ELOG post %s failed (%d): %s", entry["id"], entry["attempts"], entry["error"])
            return False
        entry.update(msg_id=msg_id, posted=time.time(), error=None)
        path.write_text(json.dumps(entry), encoding="utf-8")
        if entry["kind"] == "run_start" and entry.get("run_uid"):
            self._save_run_index(entry["run_uid"], entry["logbook"], msg_id, entry["attributes"])
        self._move(path, att_dir, "sent")
        log.info("ELOG %s #%s: %s", entry["logbook"], msg_id, entry["subject"])
        return True

    def _find_posted(self, book, entry_id: str, newest: int = 10):
        """ELOG id of the entry carrying our marker among the newest entries, else None."""
        try:
            for msg_id in sorted(book.get_message_ids(timeout=30), reverse=True)[:newest]:
                message, _, _ = book.read(msg_id, timeout=30)
                if f"{self.MARKER}{entry_id}" in message:
                    return int(msg_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("ELOG lookup of %s failed: %s", entry_id, exc)
        return None

    def _move(self, path: Path, att_dir: Path, target: str) -> None:
        path.replace(self.dir / target / path.name)
        if att_dir.exists():
            try:
                shutil.move(str(att_dir), str(self.dir / target / att_dir.name))
            except OSError:  # Windows: an attachment still open (a failed upload) - copy, clean up later
                shutil.copytree(att_dir, self.dir / target / att_dir.name, dirs_exist_ok=True)
                shutil.rmtree(att_dir, ignore_errors=True)

    def run_forever(self, stop: threading.Event | None = None, period_s: float = 2.0) -> None:
        stop = stop or threading.Event()
        backoff = period_s
        while not stop.is_set():
            ok = True
            pending = sorted((self.dir / "pending").glob("*.json"))
            for path in pending:
                if stop.is_set():
                    break
                try:
                    ok = self.post_one(path) and ok
                except Exception:  # noqa: BLE001 - one bad entry must not stop the service
                    log.exception("ELOG entry %s", path.name)
                    ok = False
            backoff = period_s if ok else min(backoff * 2, 120.0)
            # heartbeat for the System dashboard
            (self.dir / "poster.json").write_text(json.dumps(
                {"time": time.time(), "pid": os.getpid(), "url": self.cfg.get("url"), "ok": ok,
                 "pending": len(list((self.dir / "pending").glob("*.json"))), "retry_s": backoff}), encoding="utf-8")
            stop.wait(backoff)


# ------------------------------------------------------------------ Bluesky callback
class ElogRunCallback:
    """Run start -> new entry in the experiment logbook; stop -> reply."""

    def __init__(self, lab: dict) -> None:
        self.lab = lab
        self._starts: dict = {}
        self._events: dict = {}

    def __call__(self, name: str, doc: dict) -> None:
        try:
            getattr(self, name, lambda d: None)(doc)
        except Exception:  # never disturb a run because of the notebook
            log.exception("ELOG callback failed on %s", name)

    def start(self, doc: dict) -> None:
        from fastq.session import jsonl_name

        self._starts[doc["uid"]] = doc
        skip = {"uid", "time", "versions", "plan_type", "plan_args", "hints", "sequence", "plan_request", "code", "roles",
                "comment"}
        params = "\n".join(f"  {k}: {v}" for k, v in sorted(doc.items()) if k not in skip)
        req, code = doc.get("plan_request") or {}, doc.get("code") or {}
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(doc["time"]))
        text = (f"{doc['comment']}\n\n" if doc.get("comment") else "") + (
                f"Run {doc.get('scan_id', '')} started {started}\nplan: {doc.get('plan_name')}\nuid: {doc['uid']}\n"
                f"raw data: {jsonl_name(doc)}\n\n{params}")
        attachments = []
        if req:
            call = {"plan": req.get("name"), "kwargs": req.get("kwargs"), "roles": doc.get("roles"),
                    "code_version": code.get("version"), "plan_file": code.get("plan_file"), "run_uid": doc["uid"],
                    "scan_id": doc.get("scan_id")}
            args = ", ".join(f"{k}={json.dumps(v)}" for k, v in (req.get("kwargs") or {}).items())
            text += ("\n\nPlan call (all parameters; dashboard Queue page: 'fill parameters from an earlier run'):\n"
                     f"  {req.get('name')}({args})\n")
            if doc.get("roles"):
                text += f"  roles: {json.dumps(doc['roles'])}\n"
            if code.get("version"):
                text += f"  code version {code['version']} ({code.get('archive')}), plan in {code.get('plan_file')}\n"
            attachments.append((f"run{doc.get('scan_id', '')}_plan_request.json", json.dumps(call, indent=1).encode()))
            if code.get("version") and str(code.get("plan_file", "")).endswith(".py"):
                try:
                    from fastq.provenance import archived_file

                    src = archived_file(self.lab, code["version"], code["plan_file"])
                    attachments.append((f"run{doc.get('scan_id', '')}_{Path(code['plan_file']).name}", src))
                except Exception:  # noqa: BLE001
                    log.warning("plan source %s not in archive %s", code.get("plan_file"), code.get("version"))
        attrs = {"category": auto_category(self.lab, "run"), "run": doc.get("scan_id"), "sample": doc.get("sample"),
                 "file": jsonl_name(doc), "plan": doc.get("plan_name"), "status": "running"}
        subject = f"Run {doc.get('scan_id', '')}: {doc.get('plan_name')}"
        if doc.get("comment"):
            subject += f" - {doc['comment'][:80]}"
        post_entry(self.lab, doc.get("elog_logbook"), subject, text, attrs, attachments=attachments,
                   run_uid=doc["uid"], kind="run_start")

    def descriptor(self, doc: dict) -> None:
        self._events[doc["uid"]] = [doc["run_start"], doc["name"], 0]

    def event(self, doc: dict) -> None:
        if doc["descriptor"] in self._events:
            self._events[doc["descriptor"]][2] += 1

    def stop(self, doc: dict) -> None:
        start = self._starts.pop(doc["run_start"], {})
        counts = {n: c for rs, n, c in self._events.values() if rs == doc["run_start"]}
        self._events = {k: v for k, v in self._events.items() if v[0] != doc["run_start"]}
        duration = doc["time"] - start.get("time", doc["time"])
        text = (f"Run {start.get('scan_id', '')} finished: {doc['exit_status']}"
                + (f" ({doc.get('reason')})" if doc.get("reason") else "")
                + f"\nduration: {duration / 60:.1f} min\nevents per stream:\n"
                + "\n".join(f"  {k}: {v}" for k, v in sorted(counts.items())))
        post_entry(self.lab, start.get("elog_logbook"), f"Run {start.get('scan_id', '')} {doc['exit_status']}",
                   text, {"category": auto_category(self.lab, "run"), "status": doc["exit_status"]},
                   run_uid=doc["run_start"], kind="run_stop")


# ------------------------------------------------------------------ device notebooks
class DeviceEventWatcher:
    """Posts important device events to each device's logbook
    (lab.yaml elog.device_logbooks: {device name: logbook})."""

    MIN_INTERVAL_S = 60.0   # per device and event type

    WATCH = {   # device type -> [(PV suffix, event name, predicate(old, new) -> message or None)]
        "*": [("Connected", "connection", lambda o, n: None if o is None or o == n else
               ("instrument connection restored" if n in ("On", 1) else "instrument connection LOST")),
              ("IOC:STARTTOD", "ioc_restart", lambda o, n: None if o is None or o == n else f"IOC restarted ({n})"),
              # every devkit IOC: what went wrong last (a refused put, a failed driver call)
              ("LastError", "error", lambda o, n: f"error: {n}" if o is not None and n and n != o else None)],
    }   # + every param whose IOC schema declares an event (devkit Param(event=...)), see _schema_events
    RESCAN_S = 60.0   # devices that appear later (IOC started, new host) are watched too

    def __init__(self, lab: dict) -> None:
        self.lab = lab
        self.books = (elog_config(lab).get("device_logbooks") or {})
        self._last: dict = {}
        self._posted: dict = {}
        self._subs: dict = {}   # PV -> (sub, cb)

    def start(self) -> None:
        from caproto.threading.client import Context

        self.ctx = Context()
        self.scan()
        log.info("device events: watching %d PVs for %s", len(self._subs), sorted(self.books))

        def loop():
            while True:
                time.sleep(self.RESCAN_S)
                try:
                    self.scan()
                except Exception:  # noqa: BLE001
                    log.exception("device events: rescan failed")
        threading.Thread(target=loop, daemon=True, name="device-events").start()

    def scan(self) -> None:
        from caproto import ChannelType

        from fastq.discovery import device_name, read_directory

        for entry in read_directory():
            name = device_name(entry["prefix"])
            if name not in self.books or not entry["enabled"]:
                continue
            for suffix, event, pred in self.WATCH["*"] + self._schema_events(entry["prefix"]):
                key = entry["prefix"] + suffix
                if key in self._subs:
                    continue
                try:
                    (pv,) = self.ctx.get_pvs(key, timeout=5)
                    pv.wait_for_connection(timeout=5)
                    dtype = ChannelType.STRING if pv.channel.native_data_type in (ChannelType.ENUM,) else None
                    sub = pv.subscribe(data_type=dtype) if dtype is not None else pv.subscribe()
                    cb = self._callback(name, entry["prefix"], suffix, event, pred)
                    sub.add_callback(cb)
                    self._subs[key] = (sub, cb)
                except Exception as exc:  # noqa: BLE001 - retried at the next scan
                    log.debug("device events: cannot watch %s: %s", key, exc)

    def _schema_events(self, prefix: str) -> list:
        """(PV suffix, event, predicate) for the params the IOC marks: event "change" = a new non-empty
        value (an error list), any other text = the value becoming that (State "Error", Quenched "On")."""
        try:
            (pv,) = self.ctx.get_pvs(prefix + "IOC:SCHEMA", timeout=5)
            schema = json.loads(bytes(pv.read(timeout=5).data).rstrip(b"\0").decode())
        except Exception:  # noqa: BLE001 - not reachable now: the next scan tries again
            return []
        out = []
        for p in schema.get("params", []):
            ev = p.get("event")
            if not ev:
                continue
            suffix = p["name"] + ("_RBV" if p.get("rbv") else "")
            if ev == "change":
                pred = (lambda name: lambda o, n: f"{name}: {n}" if n and n != o else None)(p["name"])
            else:
                pred = (lambda name, want: lambda o, n: f"{name} = {n}" if str(n) == want != str(o) else None)(
                    p["name"], ev)
            out.append((suffix, f"{p['name']}_event", pred))
        return out

    def _callback(self, device: str, prefix: str, suffix: str, event: str, pred):
        key = prefix + suffix

        def cb(sub, response):
            data = response.data
            if len(data) and isinstance(data[0], bytes):
                value = data[0].decode(errors="replace")
            elif str(getattr(data, "dtype", "")) == "uint8":  # CA char array = long string
                value = bytes(bytearray(data)).split(b"\x00", 1)[0].decode(errors="replace")
            else:
                value = data[0].item() if len(data) and hasattr(data[0], "item") else (data[0] if len(data) else None)
            old = self._last.get(key)
            self._last[key] = value
            msg = pred(old, value)
            if msg and time.time() - self._posted.get((device, event), 0) >= self.MIN_INTERVAL_S:
                self._posted[(device, event)] = time.time()
                post_entry(self.lab, self.books[device], f"{device}: {msg}",
                           f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {device} ({prefix})\n{msg}\nPV {key} = {value!r}",
                           {"category": auto_category(self.lab, "device_event")}, kind="device_event")
        return cb


def main() -> None:
    """ELOG service: outbox poster + device event watcher (python -m fastq.elog)."""
    from fastq.session import load_lab

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("caproto").setLevel(logging.WARNING)
    lab = load_lab()
    if lab.get("directory"):
        os.environ.setdefault("FASTQ_DIRECTORY", lab["directory"])
    cfg = elog_config(lab)
    if not cfg.get("url"):
        raise SystemExit("lab.yaml has no elog.url - ELOG service not started")
    if cfg.get("device_events", True):
        try:
            DeviceEventWatcher(lab).start()
        except Exception:
            log.exception("device event watcher not started")
    log.info("ELOG poster: %s, outbox %s, current experiment %s", cfg["url"], elog_dir(lab), current_experiment(lab))
    Poster(lab).run_forever()


if __name__ == "__main__":
    main()
