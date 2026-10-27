"""Value history of every device (dashboard trends that survive page changes and restarts).

The web server records each numeric readable param of every enabled device into
<data>/history/history.db (SQLite, WAL):

  samples   (ch, t, v)                    raw values, at most one per period_s per channel,
                                          only when the value changed (or every HEARTBEAT_S)
  minutes   (ch, t, vmin, vmean, vmax, n) one row per channel and minute, built from EVERY
                                          update (the envelope stays true even when raw rows are thinned)

Retention (settings.json next to the db, edited from the dashboard; lab.yaml history: gives defaults):
  {"default": {"period_s": 10, "raw_days": 7, "keep_days": 365},
   "devices": {"itc1": {"period_s": 1, "raw_days": 30}}}
raw rows older than raw_days and minute rows older than keep_days are deleted every hour.
Queries pick raw rows for short spans and minute envelopes for long ones, decimated to ~points.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("fastq.history")

DEFAULTS = {"period_s": 10.0, "raw_days": 7.0, "keep_days": 365.0}
HEARTBEAT_S = 600.0      # an unchanged value is still stored this often (shows the channel was alive)
FLUSH_S = 2.0
PRUNE_S = 3600.0


def history_dir(lab: dict) -> Path:
    from fastq.session import data_root

    d = data_root(lab) / "history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_settings(lab: dict) -> dict:
    s = {"default": {**DEFAULTS, **((lab.get("history") or {}).get("default") or {})},
         "devices": dict((lab.get("history") or {}).get("devices") or {})}
    f = history_dir(lab) / "settings.json"
    try:
        saved = json.loads(f.read_text(encoding="utf-8"))
        s["default"].update(saved.get("default") or {})
        s["devices"].update(saved.get("devices") or {})
    except (OSError, ValueError):
        pass
    return s


def save_settings(lab: dict, settings: dict) -> dict:
    clean = {"default": {}, "devices": {}}
    for k in DEFAULTS:
        v = (settings.get("default") or {}).get(k)
        if v is not None:
            clean["default"][k] = _positive(k, v)
    for dev, d in (settings.get("devices") or {}).items():
        entry = {k: _positive(k, v) for k, v in (d or {}).items() if k in DEFAULTS and v not in (None, "")}
        if entry:
            clean["devices"][dev] = entry
    f = history_dir(lab) / "settings.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=1), encoding="utf-8")
    tmp.replace(f)
    return load_settings(lab)


def _positive(key: str, v) -> float:
    v = float(v)
    if not v > 0:
        raise ValueError(f"history {key} must be > 0")
    return v


class Recorder:
    """Thread-safe: add() is called from Channel Access callbacks, a writer thread batches the inserts."""

    def __init__(self, lab: dict) -> None:
        self.lab = lab
        self.path = history_dir(lab) / "history.db"
        self.settings = load_settings(lab)
        self._lock = threading.Lock()
        self._pending: list[tuple] = []
        self._channels: dict[str, dict] = {}   # pv -> {"id", "device", "last_t", "last_v", "minute": [...]}
        self._ids: dict[str, int] = {}
        self._stop = threading.Event()
        db = self._db()
        db.executescript("""
            CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY, pv TEXT UNIQUE, device TEXT, units TEXT);
            CREATE TABLE IF NOT EXISTS samples (ch INTEGER, t REAL, v REAL, PRIMARY KEY (ch, t)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS minutes (ch INTEGER, t REAL, vmin REAL, vmean REAL, vmax REAL, n INTEGER,
                                                PRIMARY KEY (ch, t)) WITHOUT ROWID;
        """)
        for cid, pv in db.execute("SELECT id, pv FROM channels"):
            self._ids[pv] = cid
        db.close()
        self._thread = threading.Thread(target=self._writer, daemon=True, name="history-writer")
        self._thread.start()

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    def reload_settings(self) -> None:
        self.settings = load_settings(self.lab)

    def _cfg(self, device: str) -> dict:
        return {**self.settings["default"], **self.settings["devices"].get(device, {})}

    def channel(self, pv: str, device: str, units: str = "") -> None:
        with self._lock:
            if pv not in self._channels:
                self._channels[pv] = {"device": device, "units": units, "last_t": 0.0, "last_v": None, "minute": None}

    def channels(self) -> set[str]:
        with self._lock:
            return set(self._channels)

    def add(self, pv: str, value, ts: float | None) -> None:
        if isinstance(value, bool):
            value = float(value)
        if not isinstance(value, (int, float)) or (isinstance(value, float) and math.isnan(value)):
            return
        t = float(ts or time.time())
        with self._lock:
            c = self._channels.get(pv)
            if c is None:
                return
            m = c["minute"]
            minute = math.floor(t / 60.0) * 60.0
            if m is None or m[0] != minute:
                if m is not None:
                    self._pending.append(("m", pv, m[0], m[1], m[2] / m[4], m[3], m[4]))
                c["minute"] = [minute, value, value, value, 1]
            else:
                m[1] = min(m[1], value); m[2] += value; m[3] = max(m[3], value); m[4] += 1
            period = self._cfg(c["device"])["period_s"]
            if t - c["last_t"] >= period and (value != c["last_v"] or t - c["last_t"] >= HEARTBEAT_S):
                c["last_t"], c["last_v"] = t, value
                self._pending.append(("s", pv, t, value))

    def _id(self, db, pv: str) -> int:
        cid = self._ids.get(pv)
        if cid is None:
            c = self._channels.get(pv, {})
            db.execute("INSERT OR IGNORE INTO channels (pv, device, units) VALUES (?, ?, ?)",
                       (pv, c.get("device", ""), c.get("units", "")))
            cid = db.execute("SELECT id FROM channels WHERE pv = ?", (pv,)).fetchone()[0]
            self._ids[pv] = cid
        return cid

    def _writer(self) -> None:
        db = self._db()
        last_prune = 0.0
        while not self._stop.wait(FLUSH_S):
            with self._lock:
                batch, self._pending = self._pending, []
            try:
                if batch:
                    with db:
                        for row in batch:
                            cid = self._id(db, row[1])
                            if row[0] == "s":
                                db.execute("INSERT OR REPLACE INTO samples VALUES (?, ?, ?)", (cid, row[2], row[3]))
                            else:
                                db.execute("INSERT OR REPLACE INTO minutes VALUES (?, ?, ?, ?, ?, ?)", (cid, *row[2:]))
                if time.time() - last_prune > PRUNE_S:
                    last_prune = time.time()
                    self._prune(db)
            except Exception:  # noqa: BLE001 - never stop recording because of one bad batch
                log.exception("history write failed")

    def _prune(self, db) -> None:
        now = time.time()
        by_dev: dict[str, list[int]] = {}
        for cid, dev in db.execute("SELECT id, device FROM channels"):
            by_dev.setdefault(dev, []).append(cid)
        with db:
            for dev, ids in by_dev.items():
                cfg = self._cfg(dev)
                marks = ",".join("?" * len(ids))
                db.execute(f"DELETE FROM samples WHERE ch IN ({marks}) AND t < ?", (*ids, now - cfg["raw_days"] * 86400))
                db.execute(f"DELETE FROM minutes WHERE ch IN ({marks}) AND t < ?", (*ids, now - cfg["keep_days"] * 86400))

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ queries
    def query(self, pv: str, start: float, end: float, points: int = 1000) -> dict:
        """{"t": [...], "v": [...], "vmin"/"vmax": [...] (envelope, long spans), "source": "raw"|"minutes"}"""
        db = self._db()
        try:
            row = db.execute("SELECT id FROM channels WHERE pv = ?", (pv,)).fetchone()
            out = {"pv": pv, "t": [], "v": [], "source": "raw"}
            if row is None:
                return out
            cid = row[0]
            n_raw = db.execute("SELECT COUNT(*) FROM samples WHERE ch = ? AND t BETWEEN ? AND ?", (cid, start, end)).fetchone()[0]
            oldest_raw = db.execute("SELECT MIN(t) FROM samples WHERE ch = ?", (cid,)).fetchone()[0]
            raw_covers = oldest_raw is not None and oldest_raw <= start + 120
            if n_raw <= 3 * points and (raw_covers or end - start < 7200):
                rows = db.execute("SELECT t, v FROM samples WHERE ch = ? AND t BETWEEN ? AND ? ORDER BY t", (cid, start, end)).fetchall()
                step = max(1, len(rows) // points)
                rows = rows[::step]
                out.update(t=[r[0] for r in rows], v=[r[1] for r in rows])
                return out
            bucket = max(60.0, (end - start) / points)
            rows = db.execute("""SELECT CAST(t / ? AS INTEGER) AS b, MIN(t), MIN(vmin), SUM(vmean * n) / SUM(n), MAX(vmax)
                                 FROM minutes WHERE ch = ? AND t BETWEEN ? AND ? GROUP BY b ORDER BY b""",
                              (bucket, cid, start, end)).fetchall()
            out.update(source="minutes", t=[r[1] for r in rows], v=[r[3] for r in rows],
                       vmin=[r[2] for r in rows], vmax=[r[4] for r in rows])
            return out
        finally:
            db.close()

    def stats(self) -> dict:
        db = self._db()
        try:
            return {"channels": db.execute("SELECT COUNT(*) FROM channels").fetchone()[0],
                    "samples": db.execute("SELECT COUNT(*) FROM samples").fetchone()[0],
                    "minutes": db.execute("SELECT COUNT(*) FROM minutes").fetchone()[0],
                    "size_mb": round(sum(p.stat().st_size for p in self.path.parent.glob("history.db*")) / 1e6, 1),
                    "recording": len(self._channels), "settings": self.settings}
        finally:
            db.close()
