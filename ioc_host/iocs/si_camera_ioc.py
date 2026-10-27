"""IOC: Spectral Instruments "HP easyCAM" (Illustra) cooled CCD - spectrometer camera, Camera Link + EDT
frame grabber - via the Jireh Jsdk SDK (ctypes, Jsdk.dll).

    python ioc_host/iocs/si_camera_ioc.py --camera-index 1 --prefix FDM:CAMMAIN: --data-root D:/Data/fastq
    python ioc_host/iocs/si_camera_ioc.py --camera-index 2 --prefix FDM:CAMREF: --data-root D:/Data/fastq
    python ioc_host/iocs/si_camera_ioc.py --camera-index 1 --prefix FDM:CAMMAIN: --data-root D:/Data/fastq \
        --also "--camera-index 2 --prefix FDM:CAMREF:"         # both cameras from ONE process (as 4dmag)
    python ioc_host/iocs/si_camera_ioc.py --mock --prefix TEST:CAM1: --data-root sim/data/detectors

Same detector PVs as every camera (ioc_host/common/camera.py) plus the CCD
settings below. Port of 4dmag fdm/easycam_multi.py: one Acquire = discard
stale frames, CameraStart, poll CheckData until a frame (or timeout / Abort),
GetData + ImageGetArrayU16. The frame buffer is int16 in the SDK call but
holds unsigned counts (4dmag converted to uint16 when reading back) - it is
published as uint16.

What the Jsdk Python SDK (2023, the version 4dmag used) offers, and therefore
this IOC: the camera variables of Jsdk_paramID through CameraSetID/GetID
(temperature, cooling, fan, trigger, shutter, clears, ADC offset, gain factor,
sensor info), one readout ROI with serial (x) and parallel (y) binning through
CameraROISetup, and the acquisition through ExperimentSetup/Initialize/Start/
CheckData/GetData. Its header defines ADC speed, ADC gain, readout port and
clear-mode enums, but no parameter ID or function to set them - they are not
exposed here (use the vendor's software to change them).

--jsdk-path is the folder with Jsdk.dll and its runtime files (license, .r3x,
clcamiface3.dll, SIcamCL.ini). The vendor's Python wrappers (fdm/jireh/Jsdk_*.py,
"All rights reserved") are NOT imported: they load the DLL from a fixed path at
import time; the constants needed are below and the prototypes are declared
from their documented C signatures. The working directory is changed to
--jsdk-path because the SDK looks for its .ini/license files there.

Acquisition mode (--acq-mode / AcqMode): 2 = synchronous N-frame (what
4dmag_main.py ran, FD['CameraMode'] = 2), 3 = asynchronous single frame (the
easycam_multi.setExperiment default). Changing a readout setting makes the
next Acquire redo ExperimentSetup + Initialize (as 4dmag did at run start);
thermal settings (setpoint, cooler, fan) do not.

Nothing is written at startup: 4dmag applied its startup params on connect
(exposure 3 s, external trigger, TargetCounts 200, ...); here that is the
ApplyLabDefaults action (without the 20 C temperature setpoint 4dmag also sent).
The readout ROI cannot be read back from the SDK: CcdRoi is '' until it is set
here (the camera then reads out what it was last given - normally full frame).

Two cameras: 4dmag drove MainCam (index 1) and RefCam (index 2) from ONE
process. Two IOC processes each loading Jsdk.dll on one EDT/Jsdk installation
is untested - if the SDK refuses, serve both from one process with --also
(see above); each camera keeps its own PV prefix, lock and connection.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.camera import Camera, SimCamera  # noqa: E402
from ioc_host.common.devkit import Action, Param, run_instrument  # noqa: E402

log = logging.getLogger("si_camera")

JSDK_PATH = r"C:/Jireh/JSDK for Python/64 bit/Runtime Files"  # where the vendor wrappers load it from
# Jsdk_paramID values (Jsdk_Defs.py)
MIN_BLOCK, NUM_MIN_BLOCK, STRIPS_PER_CLR, EDGE_TRIGGER, GAIN_FACTOR, TARGET_COUNTS = 0, 1, 2, 3, 4, 5
SHUTTER_TYPE, SHUTTER_POLARITY, SHTR_OPEN_DELAY, SHTR_CLOSE_DELAY, SHTR_OPEN_MODE = 6, 7, 8, 9, 10
FAN_ENABLE, COOLING_ENABLE, EXPOSURE_MODE, TEMP, TEMP_SETPOINT, EXPOS_SEC = 11, 12, 13, 14, 15, 16
TRIGGER_LINE, FW_VERSION, FPGA_VERSION, Y_SIZE, X_SIZE, PIX_Y_SIZE, PIX_X_SIZE = 18, 22, 23, 24, 25, 26, 27
BIT_DEPTH, SERIAL_NUM, CLR_MIN_BLOCK, CLEARS, SUBSTRATE_BIAS = 32, 33, 34, 35, 36
THERMAL = {FAN_ENABLE, COOLING_ENABLE, TEMP_SETPOINT}  # do not need a new ExperimentSetup
TRIGGER_NONE, TRIGGER_EACH = 1, 3          # JsdkeTriggerModes
JI_TYPE_INT16 = 2

# JsdkErrorCode: warnings are the *_WARNING_* codes and everything >= 0x8000 (logged, not raised)
WARNING_CODES = {5021, *range(5503, 5534), 5544, 5545, 5547, 5549, 5550, 5552, *range(5567, 5576), 5578, 5580,
                 5582, 5583, 5584, *range(5585, 5590), *range(5591, 5599), 5600, 5601, 5613, 5618, 5627, 5628}
# the camera is gone: reconnect instead of retrying calls on a dead handle
CONNECTION_CODES = {5000, 5042, 5050, 5054, 5535, 5537, 5557, 5602, 5611, 5612, 5622}
ERROR_TEXT = {
    5000: "no device found", 5002: "unsupported function", 5007: "param id not supported", 5008: "param not available",
    5009: "index out of range", 5033: "error reading the .ini file", 5034: "param value out of range",
    5035: "no license file", 5036: "invalid license file", 5040: "timeout", 5042: "no device available",
    5050: "device not opened", 5054: "invalid device handle", 5501: "camera could not be opened",
    5502: "illegal trigger mode", 5525: "param id not supported by this camera",
    5526: "temperature not supported", 5534: "FIFO overflow", 5535: "communication error",
    5536: "data transfer failed", 5537: "no communications link", 5540: "read-only parameter",
    5541: "bad parameter value", 5548: "temperature not set", 5549: "temperature above max",
    5550: "temperature below min", 5553: "bad ROI dimension", 5556: "data collection error",
    5557: "camera not found", 5558: "camera initialize failed", 5559: "error setting cooling",
    5561: "x ROI not a multiple of the binning", 5562: "y ROI not a multiple of the binning",
    5565: "x out of the CCD", 5566: "y out of the CCD", 5602: "camera not alive", 5604: "data collection failed",
    5605: "data overrun", 5606: "camera not initialized", 5611: "camera opened by another application",
    5612: "camera disconnected", 5621: "camera data lost", 5622: "camera connection lost",
    5625: "exposure mode error", 5626: "ROI out of range", 5642: "exposure below minimum",
    5643: "exposure above maximum", 5645: "exposure set error",
}

ACQ_MODES = ("Sync N-frame (2)", "Async single (3)")
POLARITIES = ("Low", "High", "Negative/pulse high", "Pulse low", "Strobe high", "Strobe low", "Rising edge",
              "Falling edge")                                    # JsdkTriggerPolarity value = index
SHUTTER_TYPES = ("None", "Internal", "External", "None high", "Rolling")    # JsdkShutterType value = index
SHUTTER_POLARITIES = ("Low", "High", "Negative")
SHUTTER_MODES = ("Never", "Pre exposure", "Pre sequence", "Pre trigger", "No change")  # JsdkShutterModes - 1
ROI_PRESETS = ("Custom", "Full frame", "FVB", "8 lines")  # FVB / 8 lines = the 4dmag dashboard buttons
LAB_DEFAULTS = {EXPOS_SEC: 3.0, EXPOSURE_MODE: TRIGGER_EACH, TARGET_COUNTS: 200, CLR_MIN_BLOCK: 2, CLEARS: 1,
                SHUTTER_TYPE: 2, SHUTTER_POLARITY: 1}  # 4dmag config/devices.py
COOL_C, WARM_C, AMBIENT_C = -35.0, 20.0, 20.0
LOCK_TOLERANCE_C = 1.0

u32, P = ctypes.c_uint32, ctypes.POINTER
PROTOTYPES = {
    "Jsdk_LibInit": [], "Jsdk_CamerasInSystem": [P(ctypes.c_int)], "Jsdk_CamerasRetrieve": [u32, P(u32)],
    "Jsdk_CameraOpen": [u32, P(u32)], "Jsdk_CameraClose": [u32],
    "Jsdk_CameraROISetup": [u32] * 8, "Jsdk_CameraExperimentSetup": [u32, ctypes.c_double, ctypes.c_int, u32, u32],
    "Jsdk_CameraInitialize": [u32], "Jsdk_CameraStart": [u32], "Jsdk_CameraStop": [u32],
    "Jsdk_CameraCheckData": [u32, P(ctypes.c_int32)], "Jsdk_CameraGetData": [u32, u32, ctypes.c_int32],
    "Jsdk_CameraGetDimensions": [u32, P(u32), P(u32)],
    "Jsdk_CameraSetID": [u32, ctypes.c_int, ctypes.c_double],
    "Jsdk_CameraGetID": [u32, ctypes.c_int, P(ctypes.c_double)],
    "Jsdk_CreateImage": [u32, u32, u32, ctypes.c_int, P(u32)], "Jsdk_DestroyImage": [u32],
    "Jsdk_ImageGetArrayU16": [u32, u32, u32, u32, ctypes.c_void_p],
}
_dll = None


def load_jsdk(path: str):
    """Load Jsdk.dll once per process (LibInit must precede every other call)."""
    global _dll
    if _dll is None:
        os.add_dll_directory(path)
        os.chdir(path)
        dll = ctypes.CDLL(os.path.join(path, "Jsdk.dll"))  # cdecl
        for name, args in PROTOTYPES.items():
            getattr(dll, name).argtypes, getattr(dll, name).restype = args, ctypes.c_int
        _check(dll.Jsdk_LibInit(), "LibInit")
        _dll = dll
    return _dll


def describe(code: int) -> str:
    return f"{code} ({ERROR_TEXT.get(code & 0x7FFF, 'see JsdkErrorCode')})" if code else "0 (OK)"


def _check(code: int, what: str, drv=None) -> None:
    if not code:
        return
    if drv is not None:
        drv.last_code, drv.last_status = code, f"{time.strftime('%H:%M:%S')} {what}: {describe(code)}"
    if code >= 0x8000 or code in WARNING_CODES:
        log.warning("Jsdk_%s: warning %s", what, describe(code))
    elif code in CONNECTION_CODES:
        raise ConnectionError(f"Jsdk_{what}: error {describe(code)}")
    else:
        raise RuntimeError(f"Jsdk_{what}: error {describe(code)}")


def check_roi(roi: tuple, size: tuple[int, int] | None) -> tuple:
    """(xstart, xend, xbin, ystart, yend, ybin), 1-based inclusive; each span a multiple of its binning."""
    roi = tuple(int(v) for v in roi)
    if len(roi) != 6 or min(roi) < 1 or roi[1] < roi[0] or roi[4] < roi[3]:
        raise ValueError(f"CCD ROI needs xstart,xend,xbin,ystart,yend,ybin (1-based, end >= start), got {roi}")
    xs, xe, xb, ys, ye, yb = roi
    if size and (xe > size[0] or ye > size[1]):
        raise ValueError(f"CCD ROI {roi} beyond the {size[0]} x {size[1]} CCD")
    if (xe - xs + 1) % xb or (ye - ys + 1) % yb:
        raise ValueError(f"CCD ROI {roi}: x span {xe - xs + 1} / y span {ye - ys + 1} not a multiple of the "
                         f"binning {xb} / {yb}")
    return roi


class Driver:
    """One camera through Jsdk.dll (calls as in 4dmag easycam_multi)."""

    def __init__(self, path: str, index: int, mode: int) -> None:
        self.last_code, self.last_status = 0, "OK"
        self.dll = dll = load_jsdk(path)
        n = ctypes.c_int()
        _check(dll.Jsdk_CamerasInSystem(ctypes.byref(n)), "CamerasInSystem")
        if n.value == 0:
            raise ConnectionError("Jsdk: no camera in the system (frame grabber / power / cable?)")
        dev = u32()
        _check(dll.Jsdk_CamerasRetrieve(index, ctypes.byref(dev)), f"CamerasRetrieve({index}) of {n.value} cameras")
        self.h = u32()
        _check(dll.Jsdk_CameraOpen(dev, ctypes.byref(self.h)), "CameraOpen")
        log.info("camera index %d (device id %d, %d in system) open", index, dev.value, n.value)
        self.mode, self.roi, self.data_handle, self.buf = mode, None, None, None
        self.setup_needed = True

    def get_id(self, pid: int) -> float:
        v = ctypes.c_double()
        _check(self.dll.Jsdk_CameraGetID(self.h, pid, ctypes.byref(v)), f"CameraGetID({pid})", self)
        return v.value

    def set_id(self, pid: int, value: float) -> None:
        _check(self.dll.Jsdk_CameraSetID(self.h, pid, float(value)), f"CameraSetID({pid}, {value})", self)
        if pid not in THERMAL:  # readout settings take effect with the next ExperimentSetup + Initialize
            self.setup_needed = True

    def set_roi(self, roi: tuple) -> None:
        _check(self.dll.Jsdk_CameraROISetup(self.h, 1, *map(int, roi)), f"CameraROISetup{roi}", self)
        self.roi, self.setup_needed = tuple(roi), True

    def setup(self) -> None:
        """setExperiment: exposure + mode + 1 frame, Initialize, allocate the image."""
        d = self.dll
        _check(d.Jsdk_CameraExperimentSetup(self.h, self.get_id(EXPOS_SEC), self.mode, 1, 0),
               "CameraExperimentSetup", self)
        _check(d.Jsdk_CameraInitialize(self.h), "CameraInitialize", self)
        x, y = u32(), u32()
        _check(d.Jsdk_CameraGetDimensions(self.h, ctypes.byref(x), ctypes.byref(y)), "CameraGetDimensions", self)
        if self.data_handle is not None:
            d.Jsdk_DestroyImage(self.data_handle)
            self.data_handle = None
        handle = u32()
        _check(d.Jsdk_CreateImage(x.value, y.value, 1, JI_TYPE_INT16, ctypes.byref(handle)), "CreateImage", self)
        self.data_handle, self.buf = handle.value, np.zeros((y.value, x.value), dtype=np.int16)
        self.setup_needed = False
        log.info("experiment setup: %d x %d, mode %d", x.value, y.value, self.mode)

    # Camera driver interface
    def get_exposure(self):
        return self.get_id(EXPOS_SEC)

    def set_exposure(self, s):
        self.set_id(EXPOS_SEC, s)

    def set_trigger(self, external):
        self.set_id(EXPOSURE_MODE, TRIGGER_EACH if external else TRIGGER_NONE)

    def _frames_ready(self) -> int:
        n = ctypes.c_int32()
        _check(self.dll.Jsdk_CameraCheckData(self.h, ctypes.byref(n)), "CameraCheckData", self)
        return n.value

    def grab(self, timeout_s, abort=None):
        d = self.dll
        if self.setup_needed:
            self.setup()
        t_end = time.monotonic() + 10.0  # discard_all: drain frames left from earlier starts
        while self._frames_ready() > 0 and time.monotonic() < t_end:
            _check(d.Jsdk_CameraGetData(self.h, self.data_handle, 1), "CameraGetData (discard)", self)
        _check(d.Jsdk_CameraStart(self.h), "CameraStart", self)
        t_end = time.monotonic() + timeout_s
        while not self._frames_ready():
            if abort is not None and abort.is_set():
                d.Jsdk_CameraStop(self.h)
                raise RuntimeError("aborted")
            if time.monotonic() > t_end:
                d.Jsdk_CameraStop(self.h)
                # RuntimeError, not TimeoutError: a missing trigger must not mark the camera disconnected
                raise RuntimeError(f"no frame within {timeout_s:.1f} s (trigger missing?)")
            time.sleep(0.002)
        _check(d.Jsdk_CameraGetData(self.h, self.data_handle, 1), "CameraGetData", self)
        ny, nx = self.buf.shape
        _check(d.Jsdk_ImageGetArrayU16(self.data_handle, 1, nx, ny, self.buf.ctypes.data), "ImageGetArrayU16", self)
        self.last_code, self.last_status = 0, "OK"
        return self.buf.view(np.uint16).copy()

    def abort(self):
        _check(self.dll.Jsdk_CameraStop(self.h), "CameraStop", self)

    def close(self):
        self.dll.Jsdk_CameraStop(self.h)
        if self.data_handle is not None:
            self.dll.Jsdk_DestroyImage(self.data_handle)
            self.data_handle = None
        self.dll.Jsdk_CameraClose(self.h)


class SimDriver:
    """easyCAM simulator: 1024 x 256 CCD, ROI + binning, TEC cooling with a time constant,
    dark current halving every 6 C, residual charge when the CCD is not cleared."""

    W, H = 1024, 256
    TAU_S = 60.0          # cooling time constant
    TEC_DELTA = 60.0      # deepest cooling below ambient (fan on); without fan much less

    def __init__(self, mode: int = 2) -> None:
        self.last_code, self.last_status = 0, "OK"
        self.ids = {MIN_BLOCK: 2, NUM_MIN_BLOCK: 1, STRIPS_PER_CLR: 4, EDGE_TRIGGER: 1, GAIN_FACTOR: 1,
                    TARGET_COUNTS: 200, SHUTTER_TYPE: 0, SHUTTER_POLARITY: 1, SHTR_OPEN_DELAY: 0, SHTR_CLOSE_DELAY: 0,
                    SHTR_OPEN_MODE: 2, FAN_ENABLE: 1, COOLING_ENABLE: 0, EXPOSURE_MODE: TRIGGER_NONE,
                    TEMP: AMBIENT_C, TEMP_SETPOINT: AMBIENT_C, EXPOS_SEC: 0.05, TRIGGER_LINE: 1, FW_VERSION: 3.1,
                    FPGA_VERSION: 2.4, Y_SIZE: self.H, X_SIZE: self.W, PIX_Y_SIZE: 26.0, PIX_X_SIZE: 26.0,
                    BIT_DEPTH: 16, SERIAL_NUM: 1234, CLR_MIN_BLOCK: 2, CLEARS: 1, SUBSTRATE_BIAS: 0}
        self.mode, self.roi, self.setup_needed = mode, None, True
        self.cam = SimCamera(self.W, self.H)
        self.rng = np.random.default_rng()
        self.t_temp = time.monotonic()
        self.last_frame = None

    def _update_temp(self) -> None:
        now = time.monotonic()
        dt, self.t_temp = now - self.t_temp, now
        floor = AMBIENT_C - self.TEC_DELTA * (1.0 if self.ids[FAN_ENABLE] else 0.4)
        target = max(self.ids[TEMP_SETPOINT], floor) if self.ids[COOLING_ENABLE] else AMBIENT_C
        self.ids[TEMP] += (target - self.ids[TEMP]) * (1 - np.exp(-dt / self.TAU_S))

    def get_id(self, pid):
        if pid == TEMP:
            self._update_temp()
            return round(float(self.ids[TEMP]) + self.rng.normal(0, 0.03), 2)
        return float(self.ids[pid])

    def set_id(self, pid, value):
        if pid == TEMP_SETPOINT or pid == COOLING_ENABLE or pid == FAN_ENABLE:
            self._update_temp()  # the old settings applied until now
        self.ids[pid] = float(value)
        if pid not in THERMAL:
            self.setup_needed = True

    def set_roi(self, roi):
        self.roi, self.setup_needed = tuple(roi), True

    def get_exposure(self):
        return self.ids[EXPOS_SEC]

    def set_exposure(self, s):
        self.set_id(EXPOS_SEC, s)

    def set_trigger(self, external):
        self.set_id(EXPOSURE_MODE, TRIGGER_EACH if external else TRIGGER_NONE)

    def grab(self, timeout_s, abort=None):
        xs, xe, xb, ys, ye, yb = self.roi or (1, self.W, 1, 1, self.H, 1)
        w, h = (xe - xs + 1) // xb, (ye - ys + 1) // yb
        if (self.cam.width, self.cam.height) != (w, h):
            self.cam = SimCamera(w, h)
        self.setup_needed = False
        exposure = self.ids[EXPOS_SEC]
        if abort is not None and abort.wait(min(exposure, timeout_s)):
            raise RuntimeError("aborted")
        if abort is None:
            time.sleep(min(exposure, timeout_s))
        self._update_temp()
        # on-chip binning sums charge (signal, dark current); read noise is added once per binned pixel
        scale = exposure / 0.05 * (1 + 0.05 * self.rng.standard_normal()) * xb * yb
        dark = 50.0 * 2 ** ((self.ids[TEMP] - AMBIENT_C) / 6.0) * exposure * xb * yb
        charge = np.broadcast_to(self.cam.profile[None, :] * scale + dark, (h, w))
        signal = (charge + self.rng.normal(0, 1, (h, w)) * np.sqrt(charge)) * self.ids[GAIN_FACTOR]
        frame = signal + self.ids[TARGET_COUNTS] + self.rng.normal(0, 4.0, (h, w))
        if not self.ids[CLEARS] and self.last_frame is not None and self.last_frame.shape == frame.shape:
            frame += 0.03 * self.last_frame  # uncleared CCD: charge left from the previous frame
        self.last_frame = frame
        return frame.clip(0, 65535).astype(np.uint16)

    def abort(self):
        pass


def _id(pid, cast=float):
    return lambda s: cast(s.drv.get_id(pid))


def _enum(pid, choices, offset=0):
    def get(s):
        i = int(s.drv.get_id(pid)) - offset
        if not 0 <= i < len(choices):
            raise ValueError(f"camera returned {i + offset} for an enum of {len(choices)} values")
        return i
    return get


def _set(pid, choices=None, offset=0):
    if choices:
        return lambda s, v: s.drv.set_id(pid, choices.index(v) + offset)
    return lambda s, v: s.drv.set_id(pid, v)


class SICamera(Camera):
    TYPE = "si_camera"
    DESCRIPTION = "Spectral Instruments HP easyCAM / Illustra cooled CCD (spectrometer camera, Jsdk)"
    DEFAULT_PREFIX = "FDM:CAMMAIN:"
    _size = None

    # --- cooling (the TEC: wrong settings can condense on / thermally stress the CCD -> protected)
    Temperature = Param(float, units="C", precision=1, get=_id(TEMP), poll=5.0, overview=True, trend=True,
                        doc="CCD temperature")
    TemperatureSetpoint = Param(float, units="C", precision=1, limits=(-60.0, 25.0), get=_id(TEMP_SETPOINT),
                                set="set_temperature", poll=0, kind="config", safety="protected",
                                refresh=["Cooling", "TemperatureLocked"],
                                doc="CCD temperature setpoint; below 20 C also switches cooling on (as 4dmag)")
    Cooling = Param(bool, get=_id(COOLING_ENABLE), set=_set(COOLING_ENABLE), poll=5.0, kind="config",
                    overview=True, safety="protected",
                    doc="thermoelectric cooler on; switch off only after Warm has brought the CCD near 20 C")
    TemperatureLocked = Param(bool, get="temperature_locked", poll=5.0, kind="omitted", overview=True,
                              doc=f"cooler on and the CCD within {LOCK_TOLERANCE_C:g} C of the setpoint")
    Fan = Param(bool, get=_id(FAN_ENABLE), set=_set(FAN_ENABLE), poll=0, kind="config", safety="protected",
                doc="camera fan on (it removes the TEC heat - keep it on while cooling)")

    # --- readout geometry
    SensorWidth = Param(int, get=_id(X_SIZE, int), poll=0, kind="config", doc="CCD columns (serial direction)")
    SensorHeight = Param(int, get=_id(Y_SIZE, int), poll=0, kind="config", doc="CCD rows (parallel direction)")
    CcdRoi = Param(str, get=lambda s: ",".join(map(str, s.drv.roi)) if s.drv.roi else "", set="set_ccd_roi", poll=0,
                   kind="config", safety="safe", refresh=["CcdRoiPreset", "BinX", "BinY"],
                   doc="readout ROI 'xstart,xend,xbin,ystart,yend,ybin' (1-based, inclusive; spans a multiple "
                       "of the binning); '' = not set by this IOC (camera default, normally full frame)")
    CcdRoiPreset = Param("enum", choices=ROI_PRESETS, set="set_roi_preset", poll=0, kind="config", safety="safe",
                         get="roi_preset", refresh=["CcdRoi", "BinX", "BinY"],
                         doc="Full frame; FVB = full vertical binning (1 row); 8 lines = rows binned by height/8")
    BinX = Param(int, limits=(1, 4096), get=lambda s: s.roi_now()[2], set="set_bin_x", poll=0, kind="config",
                 safety="safe", refresh=["CcdRoi", "CcdRoiPreset"],
                 doc="serial (x, along the spectrum) binning; the ROI x span must be a multiple of it")
    BinY = Param(int, limits=(1, 4096), get=lambda s: s.roi_now()[5], set="set_bin_y", poll=0, kind="config",
                 safety="safe", refresh=["CcdRoi", "CcdRoiPreset"],
                 doc="parallel (y) binning on the chip; = ROI height gives one row (FVB)")
    PixelSize = Param(float, units="um", precision=1, get=_id(PIX_X_SIZE), poll=0, kind="config",
                      doc="pixel width (x)")

    # --- CCD cleaning before each exposure
    ClearsCount = Param(int, limits=(0, 100), get=_id(CLEARS, int), set=_set(CLEARS), poll=0, kind="config",
                        doc="CCD clear cycles (whole-chip cleans) before each acquisition (4dmag: 1; 0 leaves "
                            "residual charge in the next frame)")
    ClearStrips = Param(int, limits=(0, 1024), get=_id(CLR_MIN_BLOCK, int), set=_set(CLR_MIN_BLOCK), poll=0,
                        kind="config", doc="strips cleared before discarding during a clear (4dmag: 2)")
    StripsPerClear = Param(int, limits=(0, 4096), get=_id(STRIPS_PER_CLR, int), set=_set(STRIPS_PER_CLR), poll=0,
                           kind="config", doc="rows shifted per clear step")
    MinBlock = Param(int, limits=(0, 1024), get=_id(MIN_BLOCK, int), set=_set(MIN_BLOCK), poll=0, kind="config",
                     doc="bins summed in the serial register before discarding outside the ROI (2 is safe; "
                         "large values can bleed into the data)")
    NumMinBlock = Param(int, limits=(0, 1024), get=_id(NUM_MIN_BLOCK, int), set=_set(NUM_MIN_BLOCK), poll=0,
                        kind="config", doc="number of min blocks discarded when an ROI is set")

    # --- ADC
    TargetCounts = Param(int, limits=(0, 65535), get=_id(TARGET_COUNTS, int), set=_set(TARGET_COUNTS), poll=0,
                         kind="config", doc="ADC offset (bias) in counts; subtract it from the data (4dmag: 200)")
    GainFactor = Param(float, precision=2, get=_id(GAIN_FACTOR), set=_set(GAIN_FACTOR), poll=0, kind="config",
                       doc="JICAMERA_GAIN_FACTOR - camera dependent (the Jsdk header documents no values; "
                           "the SDK refuses unsupported ones)")
    BitDepth = Param(int, get=_id(BIT_DEPTH, int), poll=0, kind="config")
    SubstrateBias = Param(int, get=_id(SUBSTRATE_BIAS, int), poll=0, kind="config",
                          doc="CCD substrate bias DAC (0-65535; read only here - a hardware bias)")

    # --- shutter and trigger
    ShutterType = Param("enum", choices=SHUTTER_TYPES, get=_enum(SHUTTER_TYPE, SHUTTER_TYPES), set=_set(SHUTTER_TYPE,
                        SHUTTER_TYPES), poll=0, kind="config", doc="shutter output (4dmag: External)")
    ShutterPolarity = Param("enum", choices=SHUTTER_POLARITIES, get=_enum(SHUTTER_POLARITY, SHUTTER_POLARITIES),
                            set=_set(SHUTTER_POLARITY, SHUTTER_POLARITIES), poll=0, kind="config",
                            doc="shutter signal polarity (camera dependent; 4dmag: High = 1)")
    ShutterMode = Param("enum", choices=SHUTTER_MODES, get=_enum(SHTR_OPEN_MODE, SHUTTER_MODES, 1),
                        set=_set(SHTR_OPEN_MODE, SHUTTER_MODES, 1), poll=0, kind="config",
                        doc="when the shutter opens (JsdkShutterModes)")
    ShutterOpenDelay = Param(float, units="ms", precision=1, limits=(0.0, 10000.0), get=_id(SHTR_OPEN_DELAY),
                             set=_set(SHTR_OPEN_DELAY), poll=0, kind="config", doc="shutter opening time allowed")
    ShutterCloseDelay = Param(float, units="ms", precision=1, limits=(0.0, 10000.0), get=_id(SHTR_CLOSE_DELAY),
                              set=_set(SHTR_CLOSE_DELAY), poll=0, kind="config",
                              doc="wait after closing before readout")
    TriggerPolarity = Param("enum", choices=POLARITIES, get=_enum(EDGE_TRIGGER, POLARITIES),
                            set=_set(EDGE_TRIGGER, POLARITIES), poll=0, kind="config",
                            doc="external trigger edge/polarity (JsdkTriggerPolarity)")
    TriggerLine = Param(int, limits=(0, 16), get=_id(TRIGGER_LINE, int), set=_set(TRIGGER_LINE), poll=0,
                        kind="config", doc="external trigger input line number")
    AcqMode = Param("enum", choices=ACQ_MODES, get=lambda s: s.drv.mode - 2, set="set_acq_mode", poll=0,
                    kind="config", doc="Jsdk data acquisition mode (4dmag runs: 2)")

    # --- identity and status
    SerialNumber = Param(int, get=_id(SERIAL_NUM, int), poll=0, kind="config")
    FirmwareVersion = Param(float, precision=2, get=_id(FW_VERSION), poll=0, kind="config")
    FpgaVersion = Param(float, precision=2, get=_id(FPGA_VERSION), poll=0, kind="config")
    SdkStatus = Param(str, get=lambda s: s.drv.last_status, poll=2.0, kind="omitted",
                      doc="last Jsdk return code that was not OK (warnings included), 'OK' after a good frame")

    Cool = Action(lambda s: s.set_temperature(COOL_C), safety="protected", doc=f"setpoint {COOL_C:g} C + cooling on",
                  refresh=["TemperatureSetpoint", "Cooling", "TemperatureLocked"])
    Warm = Action(lambda s: s.set_temperature(WARM_C), safety="protected",
                  doc=f"setpoint {WARM_C:g} C (wait for it before switching the cooler off)",
                  refresh=["TemperatureSetpoint", "Cooling", "TemperatureLocked"])
    Reinitialize = Action("reinitialize", doc="ExperimentSetup + Initialize now (after an SDK error)",
                          refresh=["SizeX", "SizeY", "SdkStatus"])
    ApplyLabDefaults = Action("apply_lab_defaults", safety="confirm",
                              doc="4dmag settings: 3 s, external trigger, TargetCounts 200, clears 2/1, "
                                  "external shutter high, ROI 8 lines",
                              refresh=["AcquireTime", "TriggerMode", "TargetCounts", "ClearStrips", "ClearsCount",
                                       "ShutterType", "ShutterPolarity", "CcdRoi", "CcdRoiPreset", "BinX", "BinY"])

    @classmethod
    def add_arguments(cls, parser) -> None:
        super().add_arguments(parser)
        parser.add_argument("--camera-index", type=int, default=1,
                            help="Jsdk_CamerasRetrieve index (4dmag: MainCam 1, RefCam 2)")
        parser.add_argument("--jsdk-path", default=JSDK_PATH, help="folder with Jsdk.dll and its runtime files")
        parser.add_argument("--acq-mode", type=int, choices=(2, 3), default=2, help="Jsdk acquisition mode")

    def open(self) -> None:
        self.drv = SimDriver(self.args.acq_mode) if self.mock else Driver(
            self.args.jsdk_path, self.args.camera_index, self.args.acq_mode)
        self.trigger_mode = "External" if self.drv.get_id(EXPOSURE_MODE) == TRIGGER_EACH else "Internal"
        self._size = None

    def sensor_size(self) -> tuple[int, int] | None:
        if not self._size:
            w, h = int(self.drv.get_id(X_SIZE)), int(self.drv.get_id(Y_SIZE))
            self._size = (w, h) if w > 0 and h > 0 else None
        return self._size

    def full_roi(self) -> tuple:
        size = self.sensor_size() or (SimDriver.W, SimDriver.H)
        return (1, size[0], 1, 1, size[1], 1)

    def roi_now(self) -> tuple:
        return self.drv.roi or self.full_roi()

    def presets(self) -> dict:
        _, w, _, _, h, _ = self.full_roi()
        return {"Full frame": (1, w, 1, 1, h, 1), "FVB": (1, w, 1, 1, h, h), "8 lines": (1, w, 1, 1, h, max(1, h // 8))}

    def roi_preset(self) -> str:
        return next((k for k, v in self.presets().items() if v == self.drv.roi), "Custom")

    def set_temperature(self, value: float) -> None:
        self.drv.set_id(TEMP_SETPOINT, value)
        if value < 20:
            self.drv.set_id(COOLING_ENABLE, 1)

    def temperature_locked(self) -> bool:
        return bool(self.drv.get_id(COOLING_ENABLE)) and \
            abs(self.drv.get_id(TEMP) - self.drv.get_id(TEMP_SETPOINT)) <= LOCK_TOLERANCE_C

    def set_ccd_roi(self, text: str) -> None:
        self.drv.set_roi(check_roi(text.replace(" ", "").split(","), self.sensor_size()))

    def set_roi_preset(self, name: str) -> None:
        if name != "Custom":
            self.drv.set_roi(self.presets()[name])

    def _set_bin(self, axis: int, b: int) -> None:
        roi = list(self.roi_now())
        roi[axis] = int(b)
        self.drv.set_roi(check_roi(roi, self.sensor_size()))

    def set_bin_x(self, b: int) -> None:
        self._set_bin(2, b)

    def set_bin_y(self, b: int) -> None:
        self._set_bin(5, b)

    def set_acq_mode(self, value: str) -> None:
        self.drv.mode, self.drv.setup_needed = ACQ_MODES.index(value) + 2, True

    def reinitialize(self) -> None:
        if not self.mock:
            self.drv.setup()
        self.drv.setup_needed = False

    def apply_lab_defaults(self) -> None:
        for pid, value in LAB_DEFAULTS.items():
            self.drv.set_id(pid, value)
        self.trigger_mode = "External"
        self.drv.set_roi(self.presets()["8 lines"])

    def settings(self) -> dict:
        d = self.drv
        out = super().settings()
        out.update(camera_index=self.args.camera_index, ccd_roi=",".join(map(str, self.roi_now())),
                   roi_set_by_ioc=d.roi is not None, ccd_temperature_c=d.get_id(TEMP),
                   temperature_setpoint_c=d.get_id(TEMP_SETPOINT), cooling=d.get_id(COOLING_ENABLE),
                   target_counts=d.get_id(TARGET_COUNTS), gain_factor=d.get_id(GAIN_FACTOR),
                   clears=d.get_id(CLEARS), clear_strips=d.get_id(CLR_MIN_BLOCK), acq_mode=d.mode,
                   shutter_type=d.get_id(SHUTTER_TYPE), trigger_polarity=d.get_id(EDGE_TRIGGER),
                   serial_number=d.get_id(SERIAL_NUM))
        return out


if __name__ == "__main__":
    run_instrument(SICamera)
