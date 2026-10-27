"""4DMAG (HHG magneto-optics / EUV reflectivity, pump-probe) recipes.

Devices come from the lab roles (control/lab.4dmag.example.yaml): main_camera, ref_camera
(spectrometer CCDs, external trigger), pulser (Arduino pump shutter + camera
trigger), delay (ps), waveplate (deg), rotator (deg), nd (ND wheel axis of the
Arduino steppers), magnet (current, A). Every recipe is one nd_scan: the axes
below are the old 4dmag sequence.py, outermost first, and every point takes one
synchronised frame from both cameras (ROI values + main/ref ratio in the
event, full frames in the cameras' HDF5 files).

Run kinds as in the 4d00x logbooks: Static (repeat only), BG (background),
pump-probe delay scans, waveplate / detector rotation, field loops, fluence.
A run with repeats=0 repeats until "Stop" (ends cleanly, status success).
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
from bluesky import plan_stubs as bps
from bluesky import preprocessors as bpp

from fastq.plans.scan import nd_scan
from fastq.session import resolve

INTEGRATORS = Path(__file__).with_name("fdm_integrators.csv")

# the production delay sequence of 2026-10 (sequence.py): fine around t0, coarse after
DEFAULT_DELAYS = [[-10, -5, 0.5], [-5, 5, 0.1], [5, 150, 1.0]]


def _segments(segments) -> list[float]:
    """[[start, stop, step], ...] -> concatenated numpy.arange (stop excluded, as in 4dmag)."""
    return np.round(np.hstack([np.arange(a, b, s) for a, b, s in segments]), 6).tolist()


def _cams():
    return [resolve(role_name="main_camera"), resolve(role_name="ref_camera")]


def _readables(extra=()):
    """Everything worth a value at every point if it exists in this lab."""
    names = ["gas", "environment", "interlock", "temperature", "magnet", "rotator", "waveplate", "delay"]
    found = [resolve(role_name=n, required=False) for n in names]
    return [d for d in [*found, *extra] if d is not None]


def _scan(axes, md, pump_values, monitors=()):
    cams, pulser = _cams(), resolve(role_name="pulser")
    if pump_values is not None:
        axes = [*axes, {"device": pulser, "signal": "shutter", "values": list(pump_values), "label": "pump"}]
    axis_devs = {id(a.get("device")) for a in axes}
    reads = [d for d in _readables() if id(d) not in axis_devs]
    # a scan that switched the pump leaves it blocked
    safe = {pulser.shutter: 0} if pump_values is not None else None
    return (yield from nd_scan(axes, detectors=cams, readables=reads, pulser=pulser, monitors=list(monitors),
                               ratios=[[cams[0], cams[1]]], md=md, safe_state=safe))


def _repeat(repeats: int) -> list:
    return [{"repeat": int(repeats)}] if repeats != 1 else []


def fdm_static(repeats: int = 0, pump_values: list | None = None, run_kind: str = "Static"):
    """Static measurement: the same point again and again (repeats=0: until Stop).
    pump_values e.g. [0, 1] alternates pump off/on at every repeat; None leaves the shutter."""
    return (yield from _scan(_repeat(repeats), {"run_kind": run_kind}, pump_values))


def fdm_background(repeats: int = 20, close_pump: bool = True, run_kind: str = "BG"):
    """Background (BG) run: like static, pump shutter closed (unless close_pump=False -
    block the probe yourself). Recorded with run_kind=BG for the analysis."""
    pulser = resolve(role_name="pulser")
    plan = _scan(_repeat(repeats), {"run_kind": run_kind, "background": True}, None)
    if close_pump:
        old = pulser.shutter.get()
        plan = bpp.pchain(bps.mv(pulser.shutter, 0), plan)
        plan = bpp.finalize_wrapper(plan, bps.mv(pulser.shutter, old))
    return (yield from plan)


def fdm_delay_scan(delay_segments_ps: list | None = None, delays_ps: list | None = None, repeats: int = 1000,
                   pump_values: list = (1,), rotator_deg: float | None = None, waveplate_deg: float | None = None,
                   snake: bool = False):
    """Pump-probe time scan. Delays from delay_segments_ps [[start, stop, step], ...] (numpy
    arange each, like sequence.py) or an explicit list delays_ps. Order: repeat x
    delay x pump. rotator_deg / waveplate_deg: go there first (fixed during the run)."""
    vals = list(delays_ps) if delays_ps else _segments(delay_segments_ps or DEFAULT_DELAYS)
    axes = _repeat(repeats)
    for role_name, value in (("rotator", rotator_deg), ("waveplate", waveplate_deg)):
        if value is not None:
            axes.append({"device": resolve(role_name=role_name), "values": [value]})
    axes.append({"device": resolve(role_name="delay"), "values": vals, "snake": snake})
    return (yield from _scan(axes, {"run_kind": "TimeDelay"}, list(pump_values)))


def fdm_waveplate_scan(angles_deg: list | None = None, start_deg: float = 0.0, stop_deg: float = 360.0,
                       step_deg: float = 2.5,
                       repeats: int = 1, delay_ps: float | None = None, pump_values: list = (0,)):
    """Waveplate rotation (polarisation of the driving IR) - "WaveplateRotation_Static"."""
    vals = list(angles_deg) if angles_deg else np.arange(start_deg, stop_deg, step_deg).round(6).tolist()
    axes = _repeat(repeats)
    if delay_ps is not None:
        axes.append({"device": resolve(role_name="delay"), "values": [delay_ps]})
    axes.append({"device": resolve(role_name="waveplate"), "values": vals})
    return (yield from _scan(axes, {"run_kind": "WaveplateRotation"}, list(pump_values)))


def fdm_rotation_scan(positions_steps: list | None = None, start_steps: float = -185000, stop_steps: float = 185000,
                      step_steps: float = 5000, repeats: int = 1, pump_values: list = (0,)):
    """Detector / sample rotation (Nanotec rotator, in its native steps) - "DetectorRotation"."""
    vals = list(positions_steps) if positions_steps else np.arange(start_steps, stop_steps, step_steps).tolist()
    axes = [*_repeat(repeats), {"device": resolve(role_name="rotator"), "values": vals}]
    return (yield from _scan(axes, {"run_kind": "DetectorRotation"}, list(pump_values)))


def fdm_field_loop(currents_a: list = (35, -35), repeats: int = 1, delays_ps: list | None = None,
                   pump_values: list = (0,)):
    """Magnetic field (magnet PSU current, A; polarity flips through 0 A in the IOC) as the
    outer loop - e.g. [35, -35] out of plane, [40, -40] in plane. Optional delay list inside."""
    axes = [*_repeat(repeats), {"device": resolve(role_name="magnet"), "values": list(currents_a), "label": "field"}]
    if delays_ps:
        axes.append({"device": resolve(role_name="delay"), "values": list(delays_ps)})
    return (yield from _scan(axes, {"run_kind": "FieldLoop"}, list(pump_values)))


def fdm_fluence_scan(nd_positions_steps: list | None = None, start_steps: float = 0, stop_steps: float = 10000,
                     step_steps: float = 10,
                     repeats: int = 1, delay_ps: float | None = None, pump_values: list = (1,),
                     nd_signal: str = "nd", close_shutter_while_moving: bool = True):
    """Pump fluence via the ND filter wheel (Arduino stepper axis `nd_signal`). The pump
    shutter is closed while the wheel moves, as the old NDclose_and_change did."""
    vals = list(nd_positions_steps) if nd_positions_steps else np.arange(start_steps, stop_steps, step_steps).tolist()
    steppers, pulser = resolve(role_name="nd"), resolve(role_name="pulser")
    axes = _repeat(repeats)
    if delay_ps is not None:
        axes.append({"device": resolve(role_name="delay"), "values": [delay_ps]})
    axes.append({"device": steppers, "signal": nd_signal, "values": vals, "label": "pump_nd"})
    if close_shutter_while_moving:  # zip: shutter closes, then wheel moves; the pump axis reopens it
        axes[-1] = {"zip": [{"device": pulser, "signal": "shutter", "values": [0] * len(vals)},
                            {"device": steppers, "signal": nd_signal, "values": vals}], "label": "pump_nd"}
    return (yield from _scan(axes, {"run_kind": "Fluence"}, list(pump_values)))


def fdm_set_rois(csv_file: str = "", offset_main: float | None = None, offset_ref: float | None = None):
    """Load the integration ranges (4dmag Integrators.csv: id;MainSpec from;MainSpec To;RefSpec from;
    RefSpec To; row "background" = background slice) into both cameras: mean over the range minus the
    camera offset (TargetCounts unless given) minus the background mean - the old preprocess.py."""
    path = Path(csv_file) if csv_file else INTEGRATORS
    rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines(), delimiter=";"))[1:]
    rows = [r for r in rows if r and r[0].strip()]
    bg = next(r for r in rows if r[0].strip() == "background")
    main, ref = _cams()
    for cam, col, offset in ((main, 1, offset_main), (ref, 3, offset_ref)):
        if offset is None:
            target = getattr(cam, "target_counts", None)
            offset = float(target.get()) if target is not None else 0.0
        cfg = {"stat": "mean", "offset": offset, "background": [int(bg[col]), int(bg[col + 1])],
               "rois": [{"name": r[0].strip(), "x": [int(r[col]), int(r[col + 1])]} for r in rows
                        if r[0].strip() != "background"]}
        yield from bps.mv(cam.rois, json.dumps(cfg))


def fdm_live(on: bool = True):
    """Live view of both cameras (alignment; nothing saved) - the old Monitor button."""
    for cam in _cams():
        yield from bps.mv(cam.live_view, int(bool(on)))
