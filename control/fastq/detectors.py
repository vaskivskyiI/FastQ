"""Detectors that store their big data on their own PC (camera frames).

CameraDetector (used automatically for IOCs whose schema says
"detector": "camera", see ioc_host/common/camera.py):
  stage()    FileName = <run date>/<run uid8>/<device>.h5, Capture = 1
  trigger()  Acquire (one frame; put-completion = frame stored)
  read()     ArrayCounter, one scalar <cam>_<roi> per ROI (+ every other normal param);
             the spectrum is not in the event (it is in the frames)
  frames     not in the event: a StreamResource (HDF5 file, as other PCs reach
             it via the IOC's DataURI) + one StreamDatum per frame, so Tiled
             serves them as <device>_image next to the rest of the run
  unstage()  Capture = 0 (file closed)

synced_trigger_and_read: several cameras that wait for one hardware trigger
(4dmag: both spectrometer cameras on the Arduino trigger) - arm all, pulse
the trigger source until every camera has its frame, then read.
"""
from __future__ import annotations

import json
import time
import uuid

import numpy as np

from bluesky import plan_stubs as bps
from bluesky.utils import Msg

from fastq.generic import GenericDetector

_RUN = {"uid": None, "time": None}


def remember_run(name: str, doc: dict) -> None:
    """RunEngine subscriber: the current run's uid/time name the frame files."""
    if name == "start":
        _RUN.update(uid=doc["uid"], time=doc["time"])


class CameraDetector(GenericDetector):
    image_dtype = "<u2"

    def _image_key(self) -> str:
        return f"{self.name}_image"

    def stage(self):
        if hasattr(self, "live_view"):  # live view would steal frames / the lock during the run
            self.live_view.set(0).wait(10)
        self._sres = None
        self._rel = None
        self._index = self._first = 0
        return super().stage()

    def _open_file(self) -> None:
        """At the first trigger of a run (its uid is known by then - stage() runs before
        the run opens): FileName = <date>/<run uid8>/<device>.h5, Capture = 1."""
        uid = _RUN["uid"] or uuid.uuid4().hex
        stamp = time.strftime("%Y-%m-%d", time.localtime(_RUN["time"] or time.time()))
        self._rel = f"{stamp}/{uid[:8]}/{self.name}.h5"
        self.file_name.set(self._rel).wait(10)
        self.capture.set(1).wait(10)
        self._first = int(self.num_captured.get() or 0)

    def trigger(self):
        if self._rel is None:
            self._open_file()
        return super().trigger()

    def unstage(self):
        try:
            if self._rel is not None:
                self.capture.set(0).wait(30)
        finally:
            return super().unstage()

    def roi_list(self) -> list[str]:
        try:
            return [str(n) for n in json.loads(self.roi_names.get() or "[]")]
        except ValueError:
            return []

    def _roi_keys(self) -> list[str]:
        return [f"{self.name}_{n}" for n in self.roi_list()]

    def read(self):
        """ROI values as one scalar column each (<cam>_<roi>): tables store no arrays."""
        res = super().read()
        rs = res.pop(f"{self.name}_roi_sums", None)
        res.pop(f"{self.name}_spectrum", None)
        if rs is not None:
            vals = np.atleast_1d(np.asarray(rs["value"], dtype=float))
            for i, key in enumerate(self._roi_keys()):
                res[key] = {"value": float(vals[i]) if i < vals.size else float("nan"), "timestamp": rs["timestamp"]}
        return res

    def describe(self):
        d = super().describe()
        rs = d.pop(f"{self.name}_roi_sums", None)
        d.pop(f"{self.name}_spectrum", None)
        if rs is not None:
            for key in self._roi_keys():
                d[key] = {"source": rs["source"], "dtype": "number", "shape": [], "precision": 3}
        ny, nx = int(self.size_y.get() or 1), int(self.size_x.get() or 1)
        d[self._image_key()] = {"source": f"PV:{self.prefix}FullFileName", "shape": [1, ny, nx], "dtype": "array",
                                "dtype_numpy": self.data_type.get() or self.image_dtype, "external": "STREAM:"}
        return d

    def get_index(self) -> int:
        return int(self.num_captured.get() or 0) - self._first

    def collect_asset_docs(self, index=None):
        stop = self.get_index() if index is None else min(index, self.get_index())
        if stop <= self._index:
            return
        if self._sres is None:
            ny, nx = int(self.size_y.get()), int(self.size_x.get())
            uri = self.data_uri.get().rstrip("/") + "/" + self._rel
            self._sres = {"uid": str(uuid.uuid4()), "data_key": self._image_key(), "mimetype": "application/x-hdf5",
                          "uri": uri, "parameters": {"dataset": "/entry/data/data", "chunk_shape": [1, ny, nx]}}
            yield "stream_resource", self._sres
        start, self._index = self._index, stop
        yield "stream_datum", {"stream_resource": self._sres["uid"], "uid": f"{self._sres['uid']}/{start}",
                               "seq_nums": {"start": 0, "stop": 0},
                               "indices": {"start": start + self._first, "stop": stop + self._first},
                               "descriptor": ""}


def synced_trigger_and_read(cameras: list, pulser=None, readables: list = (), *, pulse_period_s: float = 0.05,
                            timeout_s: float = 60.0, stream: str = "primary", before_read=None):
    """Arm every camera (their Acquire waits for a frame), fire the pulser's
    Trigger action every pulse_period_s until all frames arrived, then read
    cameras + readables into one event. Without a pulser this is
    trigger_and_read (internal trigger). before_read(): computes derived
    signals (e.g. ROI ratios) that are among the readables."""
    group = f"synced-{uuid.uuid4().hex[:8]}"
    statuses = []
    for cam in cameras:
        statuses.append((yield Msg("trigger", cam, group=group)))
    if pulser is not None:
        t0 = time.monotonic()
        yield from bps.sleep(0.02)  # let the cameras start before the first pulse
        while not all(s.done for s in statuses):
            if time.monotonic() - t0 > timeout_s:
                raise TimeoutError(f"no frame from {[c.name for c, s in zip(cameras, statuses) if not s.done]} "
                                   f"within {timeout_s} s of trigger pulses")
            yield from bps.abs_set(getattr(pulser, pulser._pulse_attr), 1, wait=True)  # schema ophyd pulser
            yield from bps.sleep(pulse_period_s)
    yield Msg("wait", None, group=group)
    if before_read is not None:
        before_read()
    for r in readables:
        if hasattr(r, "trigger") and getattr(r, "_trigger_attr", None):
            yield from bps.trigger(r, wait=True)
    yield from bps.create(stream)
    for obj in [*cameras, *readables]:
        yield from bps.read(obj)
    yield from bps.save()
