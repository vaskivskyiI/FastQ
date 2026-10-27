"""IOC: Thorlabs scientific camera (CS165/CS2100/Kiralux/Quantalux ...) via the Thorlabs
TSI Python SDK (thorlabs_tsi_sdk + the native DLLs from ThorCam).

    python ioc_host/iocs/tsi_camera_ioc.py --serial 10001 --dll-path <ThorCam SDK dll folder> --prefix FDM:TSICAM:
    python ioc_host/iocs/tsi_camera_ioc.py --mock --prefix TEST:TSICAM: --data-root sim/data/detectors

Install (4dmag fdm/CAMERA_README.txt): ThorCam, then
`pip install thorlabs_tsi_camera_python_sdk_package.zip` (from the ThorCam folder).
Same detector PVs as every camera (ioc_host/common/camera.py) plus what the SDK
(thorlabs_tsi_sdk 0.0.8, TLCamera) offers: gain, black level, hot-pixel
correction, frame-rate control, data rate, operation mode (software / hardware
/ bulb trigger), trigger polarity, sensor ROI, binning, frames per trigger,
equal-exposure pulse (EEP), LED, TEC cable state, sensor information. The SDK
has no sensor temperature readout and no cooling setpoint: the TEC of cooled
models (Quantalux) runs whenever its power cable is in (CoolingActive shows it).
Features a model lacks read as their range maximum 0 / Supported = 0; their
params then stay at their default and a put is refused by the SDK.

One Acquire: arm if needed, drop pending frames, issue a software trigger
(Internal) or wait for the hardware trigger (External / Bulb), collect
FramesPerTrigger frames and store their SUM as one frame (uint32 when > 1).
Sensor settings (ROI, binning, trigger, data rate, ...) are changed while
disarmed - the IOC disarms for them and re-arms on the next Acquire.
4dmag's fdm/camera_class.py (a test script) wrote exposure to
exposure_time_range_us (read-only); the SDK property is exposure_time_us.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.camera import Camera, SimCamera  # noqa: E402
from ioc_host.common.devkit import Action, Param, run_instrument  # noqa: E402

log = logging.getLogger("tsi_camera")

POLARITIES = ("Active high", "Active low")
OPERATION_MODES = ("Software triggered", "Hardware triggered", "Bulb")  # OPERATION_MODE value = index
DATA_RATES = ("Other", "FPS 30", "FPS 50")                               # DATA_RATE 2, 3 (0, 1 reserved)
EEP_STATES = ("Disabled", "Active", "Inactive", "Bulb")                  # EEP_STATUS value = index


def _opt(func):
    """Read an optional feature: None (PV left as is) when the model does not support it."""
    try:
        return func()
    except Exception as exc:
        log.debug("unsupported/unreadable: %s", exc)
        return None


class Driver:
    """TLCamera wrapper (camera driver interface of camera.py)."""

    def __init__(self, serial: str, dll_path: str) -> None:
        if dll_path:
            os.environ["PATH"] = dll_path + os.pathsep + os.environ["PATH"]
            os.add_dll_directory(dll_path)
        from thorlabs_tsi_sdk.tl_camera import OPERATION_MODE, ROI, TRIGGER_POLARITY, TLCameraSDK

        self.OPERATION_MODE, self.ROI, self.TRIGGER_POLARITY = OPERATION_MODE, ROI, TRIGGER_POLARITY
        self.sdk = TLCameraSDK()
        found = self.sdk.discover_available_cameras()
        serial = serial or (found[0] if found else "")
        if serial not in found:
            self.sdk.dispose()
            raise ConnectionError(f"camera {serial!r} not found; available: {found}")
        self.cam = self.sdk.open_camera(serial)
        self.cam.image_poll_timeout_ms = 100  # get_pending_frame_or_null blocks at most this long
        self.frames_per_trigger = max(1, self.cam.frames_per_trigger_zero_for_unlimited)

    def _disarm(self):
        if self.cam.is_armed:
            self.cam.disarm()

    def get(self, name):
        return getattr(self.cam, name)

    def put(self, name, value, disarm=True):
        """Write a TLCamera property (sensor settings only while disarmed)."""
        if disarm:
            self._disarm()
        setattr(self.cam, name, value)

    def range_max(self, name):
        return getattr(self.cam, name).max

    def get_exposure(self):
        return self.cam.exposure_time_us * 1e-6

    def set_exposure(self, s):
        self.cam.exposure_time_us = int(round(s * 1e6))  # settable while armed

    def get_operation_mode(self) -> int:
        return int(self.cam.operation_mode)

    def set_operation_mode(self, mode: int):
        self._disarm()
        self.cam.operation_mode = self.OPERATION_MODE(mode)

    def set_trigger(self, external):
        self.set_operation_mode(1 if external else 0)

    def get_external(self):
        return self.get_operation_mode() != 0

    def set_frames_per_trigger(self, n):
        self._disarm()
        self.frames_per_trigger = int(n)

    def get_roi(self):
        r = self.cam.roi
        return [r.upper_left_x_pixels, r.upper_left_y_pixels, r.lower_right_x_pixels, r.lower_right_y_pixels]

    def set_roi(self, roi):
        self._disarm()
        self.cam.roi = self.ROI(*roi)

    def set_binning(self, b):
        self._disarm()
        self.cam.binx = self.cam.biny = int(b)

    def get_binning(self):
        return self.cam.binx

    def full_roi(self):
        return [0, 0, self.cam.sensor_width_pixels - 1, self.cam.sensor_height_pixels - 1]

    def get_gain_db(self):
        return self.cam.convert_gain_to_decibels(self.cam.gain)

    def get_polarity(self):
        return int(self.cam.trigger_polarity == self.TRIGGER_POLARITY.ACTIVE_LOW)

    def set_polarity(self, active_low):
        self._disarm()
        tp = self.TRIGGER_POLARITY
        self.cam.trigger_polarity = tp.ACTIVE_LOW if active_low else tp.ACTIVE_HIGH

    def get_data_rate(self) -> int:
        return int(self.cam.data_rate)

    def set_data_rate(self, value: int):
        from thorlabs_tsi_sdk.tl_camera_enums import DATA_RATE

        rate = DATA_RATE(value)
        if not self.cam.get_is_data_rate_supported(rate):
            raise ValueError(f"data rate {rate.name} not supported by this camera")
        self._disarm()
        self.cam.data_rate = rate

    def measured_fps(self):
        return self.cam.get_measured_frame_rate_fps() if self.cam.is_armed else 0.0

    def grab(self, timeout_s, abort=None):
        cam = self.cam
        if not cam.is_armed:
            cam.frames_per_trigger_zero_for_unlimited = self.frames_per_trigger
            cam.arm(max(2, self.frames_per_trigger))
        while cam.get_pending_frame_or_null() is not None:  # stale frames
            pass
        if cam.operation_mode == self.OPERATION_MODE.SOFTWARE_TRIGGERED:
            cam.issue_software_trigger()
        total, t_end = None, time.monotonic() + timeout_s
        for _ in range(self.frames_per_trigger):
            frame = cam.get_pending_frame_or_null()  # blocks up to image_poll_timeout_ms
            while frame is None:
                if abort is not None and abort.is_set():
                    raise RuntimeError("aborted")
                if time.monotonic() > t_end:
                    raise RuntimeError(f"no frame within {timeout_s:.1f} s (trigger missing?)")
                frame = cam.get_pending_frame_or_null()
            img = np.copy(frame.image_buffer).reshape(cam.image_height_pixels, cam.image_width_pixels)
            total = img if total is None else total.astype(np.uint32) + img
        return total

    def abort(self):
        self._disarm()

    def close(self):
        self._disarm()
        self.cam.dispose()
        self.sdk.dispose()


class _Range:
    def __init__(self, lo, hi):
        self.min, self.max = lo, hi


class SimDriver:
    """1440 x 1080 sensor (CS165MU-like) built on the spectrometer simulator, with black
    level, gain, hot pixels, frame-rate control, EEP and the other TLCamera properties."""

    W, H = 1440, 1080
    READOUT_S = 0.0185

    def __init__(self) -> None:
        self.exposure, self.mode, self.frames_per_trigger = 0.01, 0, 1
        self.roi, self.bin, self.polarity = [0, 0, self.W - 1, self.H - 1], 1, 0
        self.armed = False
        self.props = {"gain": 0, "black_level": 5, "is_hot_pixel_correction_enabled": False,
                      "hot_pixel_correction_threshold": 655, "is_frame_rate_control_enabled": False,
                      "frame_rate_control_value": 30.0, "is_eep_enabled": False, "is_led_on": True,
                      "is_led_supported": True, "is_eep_supported": True, "is_cooling_supported": False,
                      "is_cooling_enabled": False, "model": "CS165MU (simulated)", "serial_number": "SIM0001",
                      "firmware_version": "sim 1.0", "bit_depth": 10, "sensor_width_pixels": self.W,
                      "sensor_height_pixels": self.H, "sensor_pixel_width_um": 3.45,
                      "sensor_readout_time_ns": int(self.READOUT_S * 1e9)}
        self.ranges = {"gain_range": _Range(0, 480), "black_level_range": _Range(0, 100),
                       "hot_pixel_correction_threshold_range": _Range(0, 65535),
                       "frame_rate_control_value_range": _Range(0.9, 34.8)}
        self.data_rate = 2
        self.cam = SimCamera(self.W, 4)
        rng = np.random.default_rng(7)
        self.hot = (rng.integers(0, self.H, 40), rng.integers(0, self.W, 40))  # sensor coordinates
        self.t_last = 0.0

    def _disarm(self):
        self.armed = False

    def get(self, name):
        if name == "frame_time_us":
            return int(1e6 * self._frame_period())
        if name == "eep_status":
            if not self.props["is_eep_enabled"]:
                return 0
            return 3 if self.mode == 2 else (1 if self.exposure > self.READOUT_S else 2)
        if name in self.ranges:
            return self.ranges[name]
        return self.props[name]

    def put(self, name, value, disarm=True):
        if disarm:
            self._disarm()
        rng = self.ranges.get(name + "_range")
        if rng is not None and not rng.min <= value <= rng.max:
            raise ValueError(f"{name} {value} outside {rng.min}..{rng.max}")
        self.props[name] = type(self.props[name])(value)

    def range_max(self, name):
        return self.ranges[name].max

    def _frame_period(self):
        period = max(self.exposure, self.READOUT_S * (1.6 if self.data_rate == 2 else 1.0))
        if self.props["is_frame_rate_control_enabled"]:
            period = max(period, 1.0 / self.props["frame_rate_control_value"])
        return period

    def get_exposure(self):
        return self.exposure

    def set_exposure(self, s):
        self.exposure = float(s)

    def get_operation_mode(self):
        return self.mode

    def set_operation_mode(self, mode):
        self._disarm()
        self.mode = int(mode)

    def set_trigger(self, external):
        self.set_operation_mode(1 if external else 0)

    def get_external(self):
        return self.mode != 0

    def set_frames_per_trigger(self, n):
        self._disarm()
        self.frames_per_trigger = int(n)

    def get_roi(self):
        return list(self.roi)

    def set_roi(self, roi):
        x0, y0, x1, y1 = roi
        if x1 >= self.W or y1 >= self.H:
            raise ValueError(f"ROI {roi} beyond the {self.W} x {self.H} sensor")
        self._disarm()
        self.roi = list(roi)

    def set_binning(self, b):
        self._disarm()
        self.bin = int(b)

    def get_binning(self):
        return self.bin

    def full_roi(self):
        return [0, 0, self.W - 1, self.H - 1]

    def get_gain_db(self):
        return 0.1 * self.props["gain"]

    def get_polarity(self):
        return self.polarity

    def set_polarity(self, active_low):
        self._disarm()
        self.polarity = int(active_low)

    def get_data_rate(self):
        return self.data_rate

    def set_data_rate(self, value):
        if value not in (2, 3):
            raise ValueError(f"data rate {value} not supported by this camera")
        self._disarm()
        self.data_rate = int(value)

    def measured_fps(self):
        return 1.0 / self._frame_period() if self.armed else 0.0

    def grab(self, timeout_s, abort=None):
        self.armed = True
        x0, y0, x1, y1 = self.roi
        b = self.bin
        w, h = (x1 - x0 + 1) // b, (y1 - y0 + 1) // b
        if self.cam.width != w:
            self.cam = SimCamera(w, 4)
        self.cam.exposure = self.exposure
        wait = self._frame_period() - (time.monotonic() - self.t_last)  # frame-rate limit
        if wait > 0 and abort is not None and abort.wait(min(wait, timeout_s)):
            raise RuntimeError("aborted")
        gain = 10 ** (self.get_gain_db() / 20)
        frames = []
        for _ in range(self.frames_per_trigger):
            img = np.resize(self.cam.grab(timeout_s, abort).astype(float) / 16, (h, w)) * gain * b * b
            img += self.props["black_level"] * 4  # black level steps of ~4 counts at 10 bit
            hy, hx = (self.hot[0] - y0) // b, (self.hot[1] - x0) // b
            ok = (hy >= 0) & (hy < h) & (hx >= 0) & (hx < w)
            hot = 200 + 2000 * self.exposure * gain
            # threshold in 16-bit units (x64 at 10 bit): pixels that much above their neighbours are replaced
            p = self.props
            if not (p["is_hot_pixel_correction_enabled"] and hot * 64 > p["hot_pixel_correction_threshold"]):
                img[hy[ok], hx[ok]] += hot
            frames.append(img.clip(0, 2 ** self.props["bit_depth"] - 1).astype(np.uint16))
        self.t_last = time.monotonic()
        return frames[0] if len(frames) == 1 else np.sum(frames, axis=0, dtype=np.uint32)

    def abort(self):
        self._disarm()


def _p(name, cast=None):
    """getter of a (possibly unsupported) TLCamera property"""
    return lambda s: _opt(lambda: s.drv.get(name) if cast is None else cast(s.drv.get(name)))


def _w(name, cast=lambda v: v, disarm=True):
    return lambda s, v: s.drv.put(name, cast(v), disarm)


def _max(name):
    return lambda s: _opt(lambda: s.drv.range_max(name))


class TSICamera(Camera):
    TYPE = "tsi_camera"
    DESCRIPTION = "Thorlabs scientific camera (TSI SDK)"
    DEFAULT_PREFIX = "FDM:TSICAM:"

    # --- image settings
    Gain = Param(int, limits=(0, 1000), get=_p("gain"), set=_w("gain", int), poll=0, kind="config",
                 refresh=["GainDb"], doc="gain in camera units (0 = none; up to GainMax)")
    GainDb = Param(float, units="dB", precision=1, get=lambda s: _opt(s.drv.get_gain_db), poll=0, kind="config")
    GainMax = Param(int, get=_max("gain_range"), poll=0, kind="config", doc="0: the camera has no gain")
    BlackLevel = Param(int, limits=(0, 10000), get=_p("black_level"), set=_w("black_level", int), poll=0,
                       kind="config", doc="offset added to the pixel values (up to BlackLevelMax)")
    BlackLevelMax = Param(int, get=_max("black_level_range"), poll=0, kind="config",
                          doc="0: the camera has no black level")
    HotPixelCorrection = Param(bool, get=_p("is_hot_pixel_correction_enabled"),
                               set=_w("is_hot_pixel_correction_enabled", bool), poll=0, kind="config",
                               doc="replace hot pixels by a value from their neighbours")
    HotPixelThreshold = Param(int, limits=(0, 65535), get=_p("hot_pixel_correction_threshold"),
                              set=_w("hot_pixel_correction_threshold", int), poll=0, kind="config",
                              doc="lower = more aggressive correction (up to HotPixelThresholdMax)")
    HotPixelThresholdMax = Param(int, get=_max("hot_pixel_correction_threshold_range"), poll=0, kind="config",
                                 doc="0: the camera has no hot-pixel correction")

    # --- geometry
    SensorRoi = Param(str, get=lambda s: ",".join(map(str, s.drv.get_roi())), set="set_sensor_roi", poll=0,
                      kind="config", safety="safe", refresh=["SizeX", "SizeY"],
                      doc="'x0,y0,x1,y1' upper-left / lower-right pixel (inclusive, 0-based)")
    Binning = Param(int, limits=(1, 16), get=lambda s: s.drv.get_binning(), set=lambda s, v: s.drv.set_binning(v),
                    poll=0, kind="config", safety="safe", doc="binning in x and y")
    SensorWidth = Param(int, get=_p("sensor_width_pixels"), poll=0, kind="config")
    SensorHeight = Param(int, get=_p("sensor_height_pixels"), poll=0, kind="config")
    PixelSize = Param(float, units="um", precision=2, get=_p("sensor_pixel_width_um"), poll=0, kind="config")
    BitDepth = Param(int, get=_p("bit_depth"), poll=0, kind="config")

    # --- timing and trigger
    OperationMode = Param("enum", choices=OPERATION_MODES, get=lambda s: s.drv.get_operation_mode(),
                          set="set_operation_mode", poll=0, kind="config", refresh=["TriggerMode"],
                          doc="Bulb: the exposure lasts as long as the hardware trigger pulse")
    FramesPerTrigger = Param(int, limits=(1, 1000), get=lambda s: s.drv.frames_per_trigger,
                             set=lambda s, v: s.drv.set_frames_per_trigger(v), poll=0, kind="config",
                             doc="frames per trigger; Acquire stores their sum")
    TriggerPolarity = Param("enum", choices=POLARITIES, get=lambda s: s.drv.get_polarity(),
                            set=lambda s, v: s.drv.set_polarity(v == "Active low"), poll=0, kind="config",
                            doc="hardware trigger polarity")
    FrameRateControl = Param(bool, get=_p("is_frame_rate_control_enabled"),
                             set=_w("is_frame_rate_control_enabled", bool), poll=0, kind="config",
                             refresh=["FrameTime"], doc="limit the frame rate to FrameRate (live view, streaming)")
    FrameRate = Param(float, units="fps", precision=2, limits=(0.0, 1000.0), get=_p("frame_rate_control_value"),
                      set=_w("frame_rate_control_value", float), poll=0, kind="config", safety="safe",
                      refresh=["FrameTime"], doc="frame rate while FrameRateControl is on (up to FrameRateMax)")
    FrameRateMax = Param(float, units="fps", precision=2, get=_max("frame_rate_control_value_range"), poll=0,
                         kind="config", doc="0: the camera has no frame-rate control")
    FrameTime = Param(float, units="ms", precision=3, get=_p("frame_time_us", lambda v: v / 1000), poll=0,
                      kind="config", doc="time per frame (exposure, readout, frame-rate limit)")
    ReadoutTime = Param(float, units="ms", precision=3, get=_p("sensor_readout_time_ns", lambda v: v / 1e6), poll=0,
                        kind="config")
    MeasuredFrameRate = Param(float, units="fps", precision=2, get=lambda s: _opt(s.drv.measured_fps), poll=2.0,
                              kind="omitted", doc="frames per second arriving at the PC while armed")
    DataRate = Param("enum", choices=DATA_RATES, get="get_data_rate", set="set_data_rate", poll=0, kind="config",
                     doc="sensor readout rate of compact cameras (full-frame rate)")
    Eep = Param(bool, get=_p("is_eep_enabled"), set=_w("is_eep_enabled", bool), poll=0, kind="config",
                refresh=["EepStatus"], doc="equal-exposure pulse output (light on only while all rows expose)")
    EepStatus = Param("enum", choices=EEP_STATES, get=_p("eep_status", int), poll=0, kind="config")

    # --- hardware state and identity
    Led = Param(bool, get=_p("is_led_on"), set=_w("is_led_on", bool, disarm=False), poll=0, kind="config",
                safety="safe", doc="status LED on the back (off for light-sensitive setups)")
    CoolingActive = Param(bool, get=_p("is_cooling_enabled"), poll=10.0, kind="config",
                          doc="TEC running (its power cable is plugged in; the SDK cannot switch it)")
    CoolingSupported = Param(bool, get=_p("is_cooling_supported"), poll=0, kind="config")
    Model = Param(str, get=_p("model"), poll=0, kind="config")
    SerialNumber = Param(str, get=_p("serial_number"), poll=0, kind="config")
    FirmwareVersion = Param(str, get=_p("firmware_version"), poll=0, kind="config", max_length=256)

    FullRoi = Action(lambda s: s.drv.set_roi(s.drv.full_roi()), safety="safe", doc="ROI = whole sensor",
                     refresh=["SensorRoi"])

    @classmethod
    def add_arguments(cls, parser) -> None:
        super().add_arguments(parser)
        parser.add_argument("--serial", default="", help="camera serial number (default: first found)")
        parser.add_argument("--dll-path", default="", help="folder with the TSI SDK native DLLs")

    def open(self) -> None:
        self.drv = SimDriver() if self.mock else Driver(self.args.serial, self.args.dll_path)
        self.trigger_mode = "External" if self.drv.get_external() else "Internal"

    def set_trigger_mode(self, value):
        # TriggerMode External keeps Bulb (also externally triggered)
        if value == "External" and self.drv.get_operation_mode() == 2:
            self.trigger_mode = value
            return
        super().set_trigger_mode(value)

    def set_operation_mode(self, value: str) -> None:
        self.drv.set_operation_mode(OPERATION_MODES.index(value))
        self.trigger_mode = "Internal" if value == OPERATION_MODES[0] else "External"

    def get_data_rate(self) -> int:
        v = _opt(self.drv.get_data_rate)
        return None if v is None else {2: 1, 3: 2}.get(v, 0)

    def set_data_rate(self, value: str) -> None:
        if value == "Other":
            raise ValueError("choose FPS 30 or FPS 50")
        self.drv.set_data_rate(DATA_RATES.index(value) + 1)

    def set_sensor_roi(self, text: str) -> None:
        roi = [int(v) for v in text.replace(" ", "").split(",")]
        if len(roi) != 4 or min(roi) < 0 or roi[2] <= roi[0] or roi[3] <= roi[1]:
            raise ValueError(f"SensorRoi needs x0,y0,x1,y1 with x1 > x0, y1 > y0, got {text!r}")
        self.drv.set_roi(roi)

    def settings(self) -> dict:
        d = self.drv
        out = super().settings()
        out.update(serial=_opt(lambda: d.get("serial_number")), model=_opt(lambda: d.get("model")),
                   sensor_roi=",".join(map(str, d.get_roi())), binning=d.get_binning(),
                   operation_mode=OPERATION_MODES[d.get_operation_mode()], frames_per_trigger=d.frames_per_trigger,
                   gain=_opt(lambda: d.get("gain")), gain_db=_opt(d.get_gain_db),
                   black_level=_opt(lambda: d.get("black_level")),
                   hot_pixel_correction=_opt(lambda: d.get("is_hot_pixel_correction_enabled")),
                   bit_depth=_opt(lambda: d.get("bit_depth")))
        return out


if __name__ == "__main__":
    run_instrument(TSICamera)
