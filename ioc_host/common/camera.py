"""Cameras / area detectors as declarative instruments.

A camera IOC (subclass of Camera) only implements the driver; this base adds
the detector PVs every camera has, the same way areaDetector does:

  AcquireTime / _RBV     s     exposure
  TriggerMode / _RBV           Internal | External (camera waits for a hardware trigger)
  Acquire                      put 1 = take ONE frame (discard stale frames, start, wait);
                               the put completes when the frame is processed (ophyd trigger)
  AcquireTimeout         s     how long Acquire waits beyond the exposure (external trigger!)
  ArrayCounter, SizeX, SizeY   frame number and size
  Spectrum                     the frame summed over rows (spectrometers: the spectrum)
  Rois                         JSON regions of interest (parse_rois): sums with per-ROI background,
                               or the 4dmag form (mean - offset - background slice);
                               RoiNames / RoiSums = their values
  LiveImage, LiveWidth         downsampled frame for the dashboards
  FileName                     relative path of the HDF5 file (set by the plan at stage)
  Capture / _RBV               1 = open FileName and append every frame, 0 = close
  NumCaptured, FullFileName    frames in the open file, its absolute path on this PC
  DataRoot, DataURI            where files go on this PC (--data-root) and how other PCs reach
                               that folder (--data-uri, e.g. file://<camera-pc>/Data/fastq)
  DetectorState                Idle | Exposing | Waiting trigger | Aborted | Error
  Abort                        stops a running Acquire (reaches it while it holds the lock)
Frames are stored at full size and type only in the HDF5 file (dataset
/entry/data/data, shape (N, SizeY, SizeX), SWMR, flushed every frame, one
timestamp per frame in /entry/data/timestamps, the camera settings at file
open as attributes of /entry/instrument/camera); Bluesky runs reference them
(fastq.detectors.CameraDetector). Big data never crosses Channel Access.

Driver interface (self.drv, built in open()):
  set_exposure(s), get_exposure() -> s, set_trigger(external: bool),
  grab(timeout_s, abort) -> 2-D numpy array (discard stale, start, wait for one frame;
      abort is a threading.Event - return early with RuntimeError("aborted") when it is set),
  abort() (after an aborted grab, under the instrument lock)
A camera adds its settings to the file with settings() -> dict.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

import numpy as np

from ioc_host.common.devkit import Action, Instrument, Param

log = logging.getLogger("camera")

MAX_SPECTRUM = 8192
MAX_ROIS = 64
LIVE_MAX = 256  # live image at most LIVE_MAX x LIVE_MAX


class H5Writer:
    """Append frames to /entry/data/data of one HDF5 file (SWMR: readable while written)."""

    def __init__(self, path: Path, shape: tuple, dtype, settings: dict | None = None) -> None:
        import h5py

        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.f = h5py.File(path, "a", libver="latest")
        g = self.f.require_group("entry/data")
        if "data" in g:
            self.ds, self.ts = g["data"], g["timestamps"]
            old = self.ds.shape[1:]
            if old != shape:
                self.f.close()
                raise ValueError(f"{path}: existing frames {old} != new frame {shape}")
        else:
            self.ds = g.create_dataset("data", shape=(0, *shape), maxshape=(None, *shape), dtype=dtype,
                                       chunks=(1, *shape))
            self.ts = g.create_dataset("timestamps", shape=(0,), maxshape=(None,), dtype="f8")
        # attributes cannot be written once SWMR is on: the settings at file open
        cam = self.f.require_group("entry/instrument/camera")
        for k, v in (settings or {}).items():
            try:
                cam.attrs[k] = v
            except (TypeError, ValueError):
                cam.attrs[k] = str(v)
        self.f.swmr_mode = True

    @property
    def count(self) -> int:
        return int(self.ds.shape[0])

    def append(self, frame: np.ndarray) -> int:
        if frame.shape != self.ds.shape[1:]:  # ROI/binning changed while capturing
            raise ValueError(f"frame {frame.shape} does not fit {self.path.name} {self.ds.shape[1:]}: "
                             "set Capture = 0 (new file) before changing ROI or binning")
        n = self.count
        self.ds.resize(n + 1, axis=0)
        self.ds[n] = frame
        self.ts.resize(n + 1, axis=0)
        self.ts[n] = time.time()
        self.ds.flush()
        self.ts.flush()
        return n + 1

    def close(self) -> None:
        self.f.close()


def parse_rois(text: str) -> dict:
    """Rois JSON -> {"stat", "offset", "background", "rois"}. Two forms:
      [{"name": "H23", "x": [x0, x1], "y": [y0, y1], "bg": [bx0, bx1]}, ...]     sums, per-ROI background
      {"stat": "mean", "offset": 300, "background": [0, 20],                     4dmag preprocessing: mean
       "rois": [{"name": "H23", "x": [x0, x1]}, ...]}                             - offset - mean(background)
    """
    data = json.loads(text or "[]")
    if isinstance(data, list):
        data = {"rois": data}
    rois = [r for r in data.get("rois", []) if "x" in r][:MAX_ROIS]
    return {"stat": data.get("stat", "sum"), "offset": float(data.get("offset", 0.0)),
            "background": data.get("background"), "rois": rois}


def roi_values(frame: np.ndarray, cfg: dict) -> np.ndarray:
    mean = cfg["stat"] == "mean"
    f = frame.astype(float) - cfg["offset"]
    bg_all = None
    if cfg["background"]:
        b0, b1 = cfg["background"]
        bg_all = np.nanmean(f[:, int(b0):int(b1)])
    out = np.zeros(len(cfg["rois"]))
    for i, r in enumerate(cfg["rois"]):
        y0, y1 = r.get("y", [0, f.shape[0]])
        rows = f[int(y0):int(y1)]
        if r.get("bg"):
            b0, b1 = r["bg"]
            rows = rows - rows[:, int(b0):int(b1)].mean(axis=1, keepdims=True)
        elif bg_all is not None:
            rows = rows - bg_all
        x0, x1 = r["x"]
        region = rows[:, int(x0):int(x1)]
        out[i] = np.nanmean(region) if mean else region.sum()
    return out


class Camera(Instrument):
    TYPE = "camera"
    # event_arrays: the small arrays that stay in every event (ROI values; frames go to the HDF5 file)
    ophyd = {"trigger": {"action": "Acquire", "timeout_s": 3600.0}, "detector": "camera", "event_arrays": ["RoiSums"]}
    STATES = ("Idle", "Exposing", "Waiting trigger", "Aborted", "Error")

    AcquireTime = Param(float, units="s", precision=4, limits=(0.0, 3600.0), get="get_exposure",
                        set="set_exposure", poll=0, kind="config", overview=True, safety="safe",
                        doc="exposure time")
    TriggerMode = Param("enum", choices=("Internal", "External"), get="get_trigger_mode", set="set_trigger_mode",
                        poll=0, kind="config", overview=True,
                        doc="External: each frame waits for a hardware trigger pulse")
    AcquireTimeout = Param(float, units="s", writable=True, initial=10.0, limits=(0.0, 86400.0), kind="config",
                           safety="safe", doc="Acquire waits exposure + this long for the frame")
    LiveView = Param(bool, writable=True, initial=0, kind="omitted", overview=True, safety="safe",
                     doc="1 = acquire continuously for alignment (nothing is saved; runs switch it off)")
    DetectorState = Param("enum", choices=STATES, get=lambda s: s.state, poll=1.0, kind="omitted", overview=True,
                          doc="what the camera is doing (Waiting trigger: external trigger, exposure over)")
    LiveGrab = Param(int, get="live_grab", poll=0.5, kind="omitted", doc="live-view frame counter")
    ArrayCounter = Param(int, get=lambda s: s.counter, poll=0.5, kind="normal", overview=True, doc="frames acquired")
    SizeX = Param(int, get=lambda s: s.size[1], poll=0, kind="config")
    SizeY = Param(int, get=lambda s: s.size[0], poll=0, kind="config")
    DataType = Param(str, get=lambda s: s.dtype_str, poll=0, kind="config", doc="numpy dtype of the frames")
    Spectrum = Param("array", nelm=MAX_SPECTRUM, precision=1, get=lambda s: s.spectrum, poll=0.5, kind="normal",
                     doc="frame summed over rows")
    Rois = Param(str, writable=True, initial="[]", max_length=16384, kind="config", safety="safe",
                 doc='JSON list of {"name", "x": [x0, x1], "y", "bg"} or {"stat": "mean", "offset", '
                     '"background": [x0, x1], "rois": [...]} (see camera.py parse_rois)',
                 refresh=["RoiNames"])
    RoiNames = Param(str, get=lambda s: json.dumps([r["name"] for r in s.rois()["rois"]]), poll=0,
                     max_length=4096, kind="config")
    RoiSums = Param("array", nelm=MAX_ROIS, precision=1, get=lambda s: s.roi_values, poll=0.5, kind="normal",
                    doc="integral of each ROI (order of RoiNames)")
    LiveImage = Param("array", nelm=LIVE_MAX * LIVE_MAX, precision=1, get=lambda s: s.live, poll=0.5,
                      kind="omitted")
    LiveWidth = Param(int, get=lambda s: s.live_width, poll=0.5, kind="omitted")
    FileName = Param(str, writable=True, initial="", kind="omitted", safety="safe",
                     doc="relative path of the HDF5 file under DataRoot (set by the plan)")
    Capture = Param(bool, get=lambda s: s.capturing, set="set_capture", poll=0, kind="omitted",
                    doc="1 = append every frame to FileName (0 closes it; runs do this themselves)",
                    refresh=["NumCaptured", "FullFileName"])
    NumCaptured = Param(int, get=lambda s: s.writer.count if s.writer else 0, poll=0, kind="omitted")
    FullFileName = Param(str, get=lambda s: str(s.writer.path if s.writer else s._pending_path or ""), poll=0,
                         kind="omitted")
    DataRoot = Param(str, get=lambda s: str(s.data_root), poll=0, kind="omitted")
    DataURI = Param(str, get=lambda s: s.data_uri, poll=0, kind="omitted")

    Acquire = Action("acquire", safety="safe", doc="take one frame (put completes when it is stored)", refresh=[
        "ArrayCounter", "SizeX", "SizeY", "DataType", "Spectrum", "RoiNames", "RoiSums", "LiveImage", "LiveWidth",
        "NumCaptured", "DetectorState"])
    # lock=False: must reach an Acquire that holds the lock while it waits for a frame
    Abort = Action("request_abort", safety="safe", lock=False, doc="stop a running acquisition")

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--data-root", default=os.environ.get("FASTQ_DETECTOR_ROOT", "detector_data"),
                            help="folder for the HDF5 frame files on this PC")
        parser.add_argument("--data-uri", default="",
                            help="the same folder as other PCs reach it, e.g. file://HOST/share/fastq "
                                 "(default: file://localhost/<data-root>)")

    def __init__(self, args) -> None:
        super().__init__(args)
        self.data_root = Path(args.data_root).resolve()
        self.data_uri = args.data_uri or self.data_root.as_uri().replace("file:///", "file://localhost/")
        self.counter = 0
        self.live_frames = 0
        self.size = (0, 0)
        self.dtype_str = ""
        self.spectrum = np.zeros(1)
        self.roi_values = np.zeros(1)
        self.live, self.live_width = np.zeros(1), 1
        self.writer: H5Writer | None = None
        self._pending_path: Path | None = None  # Capture=1: file opened with the first frame (shape, type)
        self._wlock = threading.Lock()
        self._abort = threading.Event()
        self.trigger_mode = "Internal"
        self.state = "Idle"

    # ---------------------------------------------------------------- driver glue
    def get_exposure(self):
        return self.drv.get_exposure()

    def set_exposure(self, value):
        self.drv.set_exposure(value)

    def get_trigger_mode(self):
        return self.trigger_mode

    def set_trigger_mode(self, value):
        self.drv.set_trigger(value == "External")
        self.trigger_mode = value

    def rois(self) -> dict:
        try:
            return parse_rois(self.values.get("Rois") or "[]")
        except (ValueError, TypeError, AttributeError):
            return parse_rois("[]")

    def set_capture(self, on: bool) -> None:
        with self._wlock:
            if self.writer is not None:
                writer, self.writer = self.writer, None
                n = writer.count
                writer.close()
                log.info("closed %s (%d frames)", writer.path, n)
            if on:
                rel = (self.values.get("FileName") or "").strip().replace("\\", "/")
                if not rel or rel.startswith("/") or ".." in rel.split("/"):
                    raise ValueError(f"FileName must be a relative path, got {rel!r}")
                self._pending_path = self.data_root / rel
            else:
                self._pending_path = None

    @property
    def capturing(self) -> bool:
        return self.writer is not None or self._pending_path is not None

    def live_grab(self) -> int:
        """Poll hook (every 0.5 s): one frame while LiveView is on - updates the published
        spectrum / ROIs / live image, never the HDF5 file."""
        if self.values.get("LiveView") and self.writer is None and self._pending_path is None:
            self.acquire(store=False)
            self.live_frames += 1
        return self.live_frames

    def request_abort(self) -> None:
        """Runs without the lock: only signals the grab loop (the driver is not thread safe)."""
        self._abort.set()

    def _set_state(self, state: str) -> None:
        self.state = state
        try:
            self.publish(DetectorState=state)
        except Exception:  # state display only - never fail an acquisition for it
            log.debug("publish DetectorState failed", exc_info=True)

    def settings(self) -> dict:
        """Camera settings stored with the frames (subclasses add theirs)."""
        return {"type": self.TYPE, "prefix": getattr(self.args, "prefix", ""), "exposure_s": self.drv.get_exposure(),
                "trigger_mode": self.trigger_mode}

    def _settings(self) -> dict:
        try:
            return {k: v for k, v in self.settings().items() if v is not None}
        except Exception as exc:
            log.warning("camera settings for the file: %s", exc)
            return {"type": self.TYPE}

    def acquire(self, store: bool = True) -> None:
        self._abort.clear()
        exposure = float(self.drv.get_exposure() or 0.0)
        self._set_state("Exposing")
        waiting = None
        if self.trigger_mode == "External":  # no frame once the exposure is over: the trigger has not come
            waiting = threading.Timer(exposure + 0.2, lambda: self.state == "Exposing" and
                                      self._set_state("Waiting trigger"))
            waiting.daemon = True
            waiting.start()
        try:
            frame = np.asarray(self.drv.grab(exposure + float(self.values["AcquireTimeout"]), self._abort))
        except Exception:
            if waiting:
                waiting.cancel()
            aborted = self._abort.is_set()
            if aborted:
                try:
                    self.drv.abort()
                except Exception:
                    log.debug("driver abort after an aborted grab", exc_info=True)
            self._set_state("Aborted" if aborted else "Error")
            if aborted:  # deliberate: complete the put (a refused put never completes for CA clients)
                log.warning("acquisition aborted - no frame")
                return
            raise
        if waiting:
            waiting.cancel()
        self._set_state("Idle")
        if frame.ndim == 1:
            frame = frame[None, :]
        with self._wlock:
            if not store:
                pass
            elif self._pending_path is not None and self.writer is None:
                self.writer = H5Writer(self._pending_path, frame.shape, frame.dtype, self._settings())
                self._pending_path = None
                log.info("capturing to %s", self.writer.path)
            if store and self.writer is not None:
                self.writer.append(frame)
        self.counter += 1
        self.size = frame.shape
        self.dtype_str = frame.dtype.str
        self.spectrum = frame.sum(axis=0, dtype=float)[:MAX_SPECTRUM]
        cfg = self.rois()
        self.roi_values = roi_values(frame, cfg) if cfg["rois"] else np.zeros(1)
        sy, sx = (max(1, int(np.ceil(d / LIVE_MAX))) for d in frame.shape[:2])  # per axis (8 x 2048 spectra)
        live = frame[::sy, ::sx]
        self.live, self.live_width = live.astype(float).ravel(), live.shape[1]

    def close(self) -> None:
        with self._wlock:
            if self.writer is not None:
                self.writer.close()
                self.writer = None
        super().close()


class SimCamera:
    """Spectrometer camera simulator: a few HHG-like peaks on a noisy background."""

    def __init__(self, width: int = 2048, height: int = 8, seed: int | None = None) -> None:
        self.width, self.height = width, height
        self.exposure, self.external = 0.05, False
        self.rng = np.random.default_rng(seed)
        x = np.arange(width)
        self.profile = sum(a * np.exp(-0.5 * ((x - c) / 6.0) ** 2)
                           for a, c in zip((300, 800, 1200, 900, 400), np.linspace(0.2, 0.8, 5) * width))

    def set_exposure(self, s):
        self.exposure = float(s)

    def get_exposure(self):
        return self.exposure

    def set_trigger(self, external):
        self.external = bool(external)

    def grab(self, timeout_s, abort: threading.Event | None = None):
        if (abort or threading.Event()).wait(min(self.exposure, timeout_s)):
            raise RuntimeError("aborted")
        scale = self.exposure / 0.05 * (1 + 0.05 * self.rng.standard_normal())
        rows = self.profile[None, :] * scale + self.rng.normal(100, 3, (self.height, self.width))
        return rows.clip(0, 65535).astype(np.uint16)

    def abort(self):
        pass
