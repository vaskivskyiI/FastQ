"""ophyd devices built at run time from an IOC's self-description
(<prefix>IOC:SCHEMA, written by ioc_host/common/devkit.py) - a new
instrument needs no code on the control host.

    from fastq.generic import device_from_prefix
    flow = device_from_prefix("FASTQ:FLOW1:", name="flow1")
    flow.setpoint.set(1.5).wait()        # put-completion = the IOC finished the driver call
    RE(bps.mv(flow, 1.5))                # the device's positioner (if it declares one)

Mapping of a schema param (attr = snake_case of the PV name):
  read-only                -> EpicsSignalRO(Name)
  writable + readback      -> EpicsSignal(read Name_RBV, write Name, put_complete)
  writable, no readback    -> EpicsSignal(Name, put_complete)
  positioner {...}         -> set() also waits until the readback param is within
                              tolerance for settle_s (and the moving param is 0)
  action                   -> EpicsSignal(Name, put_complete); dev.<attr>_cmd.set(arg)
The device itself is a positioner when exactly one param declares
"positioner" (or schema["ophyd"]["positioner"] names it): mv(dev, x),
dev.position, usable as a scan axis. schema["ophyd"]["trigger"] = an action
name makes the device a detector: trigger() runs that action; as a dict
{"action", "timeout_s", "require": {"param", "values", "message"}} trigger()
also fails unless that param ends in one of the values (e.g. a Keithley run
State "Done"). schema["ophyd"]["stop"] = the action dev.stop() runs (abort).

Every device also has the poll-rate controls of its IOC: dev.scan (the .SCAN
field of the fastest poll group, e.g. ".2 second") and dev.fast_target /
fast_period / fast_duration / fast_active (faster than .1 s, see
ioc_host/common/fast_poll.py).
"""
from __future__ import annotations

import json
import threading
import time

from ophyd import Component as Cpt
from ophyd import Device, DeviceStatus, EpicsSignal, EpicsSignalRO, Kind
from ophyd.status import Status

KIND = {"hinted": Kind.hinted, "normal": Kind.normal, "config": Kind.config, "omitted": Kind.omitted}
_RESERVED = set(dir(Device)) | {"position", "positioner", "connected", "ioc", "schema", "scan", "fast_target",
                                 "fast_period", "fast_duration", "fast_active"}

_class_cache: dict[str, type] = {}


class IOCHealth(Device):
    """iocStats PVs (<prefix>IOC:...)."""
    ioc_type = Cpt(EpicsSignalRO, "IOC:TYPE", string=True, kind=Kind.config)
    heartbeat = Cpt(EpicsSignalRO, "IOC:HEARTBEAT", kind=Kind.omitted)
    hostname = Cpt(EpicsSignalRO, "IOC:HOSTNAME", string=True, kind=Kind.config)


class FastQDevice(Device):
    connected = Cpt(EpicsSignalRO, "Connected", kind=Kind.omitted)
    ioc = Cpt(IOCHealth, "", kind=Kind.omitted)
    fast_target = Cpt(EpicsSignal, "FastPollTarget", string=True, kind=Kind.omitted)
    fast_period = Cpt(EpicsSignal, "FastPollPeriodS", kind=Kind.omitted)
    fast_duration = Cpt(EpicsSignal, "FastPollDurationS", kind=Kind.omitted)
    fast_active = Cpt(EpicsSignal, "FastPollActive", kind=Kind.omitted)
    _stop_attr = _pulse_attr = ""

    def stop(self, *, success: bool = False) -> None:
        if self._stop_attr and not success:
            getattr(self, self._stop_attr).put(1, use_complete=False)


def read_schema(prefix: str, timeout: float = 5.0) -> dict:
    sig = EpicsSignalRO(prefix + "IOC:SCHEMA", string=True, name="schema")
    sig.wait_for_connection(timeout=timeout)
    return json.loads(sig.get())


def _wait_put(sig, start_put, limit: float, value) -> None:
    """Wait for a put to complete - or fail as soon as the IOC reports it refused it
    (CA clients never see a rejected put: it just never completes)."""
    parent = sig.parent
    errors = getattr(parent, "error_count", None)
    n0 = int(errors.get() or 0) if errors is not None else 0  # before the put: the refusal is immediate
    put_status = start_put()
    t0 = time.monotonic()
    while not put_status.done:
        if errors is not None and int(errors.get() or 0) > n0:
            msg = parent.last_error.get()
            name = sig.setpoint_pvname.split(":")[-1]
            # a refusal comes at once; later "refused" lines belong to someone else's put of the same PV
            # (a second Acquire while this one runs)
            own = f" {name}=" in msg or (f"action {name}" in msg and
                                        ("refused" not in msg or time.monotonic() - t0 < 1.0))
            if own:
                raise RuntimeError(f"{sig.name} = {value!r} refused by the IOC: {msg}")
        if time.monotonic() - t0 > limit:
            raise TimeoutError(f"{sig.name}: put of {value!r} did not complete within {limit:g} s")
        time.sleep(0.05)
    put_status.wait(0)


class CheckedSetpoint(EpicsSignal):
    """Writable param: set() completes when the IOC finished the driver call and
    fails fast when the IOC refused it (limits, instrument error)."""

    def __init__(self, *args, put_timeout: float = 120.0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.put_timeout = put_timeout

    def set(self, value, *, timeout=None, settle_time=None, **kwargs):
        limit = timeout or self.put_timeout
        status = Status(obj=self, timeout=limit + 1, settle_time=settle_time)

        def work():
            try:
                _wait_put(self, lambda: super(CheckedSetpoint, self).set(value, timeout=limit), limit, value)
                status.set_finished()
            except Exception as exc:
                status.set_exception(exc)

        threading.Thread(target=work, daemon=True, name=f"put {self.name}").start()
        return status


class SettlingSetpoint(EpicsSignal):
    """Setpoint whose set() completes when the physical value got there:
    the IOC finished the put, then |readback - target| <= tolerance held for
    settle_s, and the `moving` signal (if any) reads 0. Fails after timeout_s."""

    def __init__(self, *args, readback_attr: str | None = None, moving_attr: str | None = None,
                 tolerance: float = 0.0, settle_s: float = 0.0, timeout_s: float = 600.0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.readback_attr, self.moving_attr = readback_attr, moving_attr
        self.tolerance, self.settle_s, self.timeout_s = tolerance, settle_s, timeout_s
        self._stop_evt = threading.Event()

    def _physical(self) -> float:
        if self.readback_attr:
            return float(getattr(self.parent, self.readback_attr).get())
        return float(self.get())

    def set(self, value, *, timeout=None, settle_time=None, tolerance=None, settle_s=None, **kwargs):
        tol = self.tolerance if tolerance is None else tolerance
        settle = self.settle_s if settle_s is None else settle_s
        limit = timeout or self.timeout_s
        status = Status(obj=self, timeout=limit, settle_time=settle_time)
        self._stop_evt.clear()

        def work():
            try:
                _wait_put(self, lambda: super(SettlingSetpoint, self).set(value, timeout=limit), limit, value)
                t0, since, seen_moving = time.monotonic(), None, False
                while not status.done:
                    if self._stop_evt.is_set():
                        raise RuntimeError(f"{self.name}: stopped before reaching {value}")
                    moving = bool(self.moving_attr and int(getattr(self.parent, self.moving_attr).get() or 0))
                    seen_moving |= moving
                    if tol > 0:
                        there = abs(self._physical() - value) <= tol
                    elif self.moving_attr:  # no tolerance: the motion started and ended (or never had to start)
                        there = seen_moving or time.monotonic() - t0 > 1.0
                    else:
                        there = True
                    if not moving and there:
                        since = since or time.monotonic()
                        if time.monotonic() - since >= settle:
                            status.set_finished()
                            return
                    else:
                        since = None
                    if time.monotonic() - t0 > limit:
                        raise TimeoutError(f"{self.name}: readback {self._physical():g} not within {tol:g} "
                                           f"of {value:g} after {limit:g} s")
                    time.sleep(0.1)
            except Exception as exc:
                status.set_exception(exc)

        threading.Thread(target=work, daemon=True, name=f"set {self.name}").start()
        return status

    def stop(self, *, success=False):
        self._stop_evt.set()


def _attr(name: str) -> str:
    return name + "_" if name in _RESERVED else name


def device_class(schema: dict) -> type:
    """ophyd Device subclass for a schema (cached per type + schema text)."""
    key = json.dumps(schema, sort_keys=True)
    if key in _class_cache:
        return _class_cache[key]
    attrs: dict = {"schema": schema}
    pos_params = [p for p in schema["params"] if p.get("positioner")]
    pos_name = (schema.get("ophyd") or {}).get("positioner") or (pos_params[0]["name"] if len(pos_params) == 1 else None)
    by_name = {p["name"]: p for p in schema["params"]}
    for p in schema["params"]:
        attr, kind = _attr(p["attr"]), KIND[p.get("kind", "normal")]
        event_arrays = (schema.get("ophyd") or {}).get("event_arrays", ())
        if p["dtype"] == "array" and kind in (Kind.normal, Kind.hinted) and p["name"] not in event_arrays:
            kind = Kind.omitted  # event tables hold scalars; arrays are read on demand (or in detector files)
        string = p["dtype"] in ("str", "enum")
        if not p["writable"]:
            attrs[attr] = Cpt(EpicsSignalRO, p["name"], kind=kind, string=string)
            continue
        read_pv = p["name"] + "_RBV" if p.get("rbv") else p["name"]
        pos = p.get("positioner")
        if pos:
            rb = pos.get("readback")
            attrs[attr] = Cpt(SettlingSetpoint, read_pv, write_pv=p["name"], put_complete=True, kind=kind,
                              string=string, readback_attr=_attr(by_name[rb]["attr"]) if rb else None,
                              moving_attr=_attr(by_name[pos["moving"]]["attr"]) if pos.get("moving") else None,
                              tolerance=float(pos.get("tolerance", 0.0)), settle_s=float(pos.get("settle_s", 0.0)),
                              timeout_s=float(pos.get("timeout_s", 600.0)))
        else:
            attrs[attr] = Cpt(CheckedSetpoint, read_pv, write_pv=p["name"], put_complete=True, kind=kind,
                              string=string)
    for a in schema.get("actions", []):
        attrs[a["attr"] + "_cmd"] = Cpt(CheckedSetpoint, a["name"], put_complete=True, kind=Kind.omitted,
                                        string=a.get("arg") == "str", put_timeout=float(a.get("timeout_s", 120.0)))
    attrs["last_error"] = Cpt(EpicsSignalRO, "LastError", string=True, kind=Kind.omitted)
    attrs["error_count"] = Cpt(EpicsSignalRO, "ErrorCount", kind=Kind.omitted)
    attrs["busy"] = Cpt(EpicsSignalRO, "Busy", kind=Kind.omitted)
    leaders = schema.get("poll_leaders") or {}
    if leaders:
        fastest = min(leaders, key=leaders.get)
        attrs["scan"] = Cpt(EpicsSignal, fastest + ".SCAN", string=True, kind=Kind.omitted)
    ophyd_hints = schema.get("ophyd") or {}
    if ophyd_hints.get("pulser"):  # the action that fires one shot (fastq.detectors.synced_trigger_and_read)
        attrs["_pulse_attr"] = next(a["attr"] for a in schema["actions"] if a["name"] == ophyd_hints["pulser"]) + "_cmd"
    if ophyd_hints.get("stop"):
        attrs["_stop_attr"] = next(a["attr"] for a in schema["actions"] if a["name"] == ophyd_hints["stop"]) + "_cmd"
    bases: tuple = (FastQDevice,)
    if pos_name:
        attrs["_pos_attr"] = _attr(by_name[pos_name]["attr"])
        pp = by_name[pos_name]["positioner"]
        attrs["_pos_readback"] = _attr(by_name[pp["readback"]]["attr"]) if pp.get("readback") else attrs["_pos_attr"]
        bases = (GenericPositioner,)
    trig = (schema.get("ophyd") or {}).get("trigger")
    if trig:
        trig_name = trig["action"] if isinstance(trig, dict) else trig
        attrs["_trigger_attr"] = next(a["attr"] for a in schema["actions"] if a["name"] == trig_name) + "_cmd"
        attrs["_trigger_timeout"] = float(trig.get("timeout_s", 600.0)) if isinstance(trig, dict) else 600.0
        req = trig.get("require") if isinstance(trig, dict) else None
        if req:
            attrs["_require"] = (_attr(by_name[req["param"]]["attr"]), tuple(req["values"]),
                                 _attr(by_name[req["message"]]["attr"]) if req.get("message") else None)
        bases = (GenericDetector,) + bases
        if (schema.get("ophyd") or {}).get("detector") == "camera":
            from fastq.detectors import CameraDetector

            bases = (CameraDetector,) + bases[1:]
    cls = type(f"{schema['type'].title().replace('_', '')}Device", bases, attrs)
    _class_cache[key] = cls
    return cls


class GenericPositioner(FastQDevice):
    """Device whose set() moves its declared positioner param. Its readback is
    named like the device (data key "delay", not "delay_position"), as for motors."""
    _pos_attr = _pos_readback = ""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        getattr(self, self._pos_readback).name = self.name

    @property
    def position(self):
        return getattr(self, self._pos_readback).get()

    def set(self, value, **kwargs):
        return getattr(self, self._pos_attr).set(value, **kwargs)

    def stop(self, *, success: bool = False) -> None:
        getattr(self, self._pos_attr).stop(success=success)
        if self._stop_attr and not success:  # schema["ophyd"]["stop"], declared by the IOC
            getattr(self, self._stop_attr).put(1, use_complete=False)


class GenericDetector(FastQDevice):
    """trigger() = run the declared action (put-completion = acquisition done)."""
    _trigger_attr = ""
    _trigger_timeout = 600.0
    _require: tuple | None = None  # (attr, accepted values, message attr)

    def trigger(self):
        status = DeviceStatus(self, timeout=self._trigger_timeout)
        sig = getattr(self, self._trigger_attr)

        def work():
            try:
                sig.set(1, timeout=self._trigger_timeout).wait(self._trigger_timeout)
                if self._require:
                    attr, ok, msg = self._require
                    value = getattr(self, attr).get(use_monitor=False)
                    if value not in ok:
                        why = getattr(self, msg).get(use_monitor=False) if msg else ""
                        raise RuntimeError(f"{self.name}: {attr} = {value} after {sig.name}: {why}")
                status.set_finished()
            except Exception as exc:
                status.set_exception(exc)

        threading.Thread(target=work, daemon=True).start()
        return status


def device_from_prefix(prefix: str, name: str, schema: dict | None = None, timeout: float = 5.0):
    return device_class(schema or read_schema(prefix, timeout))(prefix, name=name)
