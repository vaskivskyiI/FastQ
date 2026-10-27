"""Declarative instruments: describe a device once, get the IOC, the ophyd
device, the dashboard profile and the AI/agent description from it.

    class FlowController(Instrument):
        TYPE = "voegtlin"
        DESCRIPTION = "Voegtlin red-y mass flow controller (Modbus RTU)"

        Flow = Param(float, units="ln/min", get="read_flow", poll=1.0, trend=True)
        Flow_SP = ...
        Setpoint = Param(float, units="ln/min", writable=True, get="read_sp", set="write_sp",
                         limits=(0, 5), doc="flow setpoint")
        Zero = Action("zero", doc="zero the sensor")

        @classmethod
        def add_arguments(cls, parser): parser.add_argument("--port", default="COM27")
        def open(self): self.drv = Driver(self.args.port)     # or the simulator with --mock
        def read_flow(self): return self.drv.flow
        ...

    if __name__ == "__main__":
        run_instrument(FlowController)

The file is ioc_host/iocs/<type>_ioc.py; the supervisor finds it, the directory
announces it, and clients build everything else from <prefix>IOC:SCHEMA - no
code on the control host or in the web UI is needed for a new instrument.

PV rules (the conventions of ioc_base.py):
  * Param read-only            -> <prefix>Name
  * Param writable with get    -> <prefix>Name (setpoint) + <prefix>Name_RBV (readback)
  * Param writable without get -> <prefix>Name only (a value the IOC keeps, e.g. a run parameter)
  * Action                     -> <prefix>Name ; a put runs the method, put-completion = method done
  * LastError, ErrorCount, Busy -> what went wrong last (CA clients never see a rejected put)
Every driver call runs in a worker thread under ONE lock per instrument
(interleaved GPIB/serial conversations desynchronise replies). Nothing is
written to the instrument at startup.

Long measurements (a Keithley run): an exclusive Action holds the lock for the
whole run and calls self.publish(Progress=..., Data=...) to update read-only
params meanwhile; lock=False Actions (Abort, Stop) still get through - they
only signal the running method (e.g. a threading.Event).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import traceback
from typing import Any, Callable

import numpy as np
from caproto import AlarmSeverity, AlarmStatus, ChannelType
from caproto.server import pvproperty

from ioc_host.common.async_utils import run_blocking
from ioc_host.common.fast_poll import FastPollGroup
from ioc_host.common.ioc_base import enum_choice, ioc_arg_parser, pv_bool, serve, setup_logging

log = logging.getLogger("devkit")

KINDS = ("hinted", "normal", "config", "omitted")
SAFETY = ("safe", "confirm", "protected")
RECONNECT_S = 10.0


def snake(name: str) -> str:
    """PV-style CamelCase -> ophyd attribute name (OutputPower -> output_power)."""
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    s = re.sub(r"(?<=[A-Z])([A-Z][a-z])", r"_\1", s)
    return s.lower()


class Param:
    """One value of an instrument.

    dtype      float | int | bool | str | "enum" (choices=...) | "array" (float waveform, nelm=...)
    get / set  method names (or callables taking the instrument) - blocking driver code
    poll       seconds between reads (None: the class POLL; 0: read once after connect and after every set);
               params with the same period are read together, the first one's .SCAN field
               controls the group's rate
    writable   the value can be put; with get -> Name + Name_RBV
    limits     (low, high) - enforced by the IOC, shown in the UI
    kind       ophyd kind: hinted (plotted) | normal (in every reading) | config | omitted
    positioner {"tolerance": .., "settle_s": .., "timeout_s": .., "readback": "OtherParam",
                "moving": "MovingParam"} - ophyd set() waits until the readback is within
                tolerance for settle_s (motors, temperatures, fields)
    overview / trend   shown on the dashboard overview card / as its trend line
    safety     how a dashboard writes it: "safe" (at once), "confirm" (default: asks first),
               "protected" (needs the lab PIN - beam, magnet, interlock, heater limits ...)
    event      an ELOG device event (fastq.elog): "change" = posted when the value changes to
               something non-empty (an error list), any other text = posted when the value
               becomes that (a State "Error")
    """

    def __init__(self, dtype: Any = float, *, units: str = "", precision: int = 4, doc: str = "",
                 get: str | Callable | None = None, set: str | Callable | None = None,
                 poll: float | None = None, writable: bool | None = None, choices: tuple | list | None = None,
                 limits: tuple | None = None, nelm: int = 1, max_length: int = 1024, kind: str = "normal",
                 initial: Any = None, positioner: dict | None = None, overview: bool = False,
                 trend: bool = False, label: str | None = None, refresh: list[str] | None = None,
                 safety: str = "confirm", event: str | None = None) -> None:
        if safety not in SAFETY:
            raise ValueError(f"safety must be one of {SAFETY}")
        self.safety, self.event = safety, event
        self.dtype = dtype
        self.refresh = refresh or []  # other params to re-read after a put of this one
        self.units, self.precision, self.doc = units, precision, doc
        self.get, self.set, self.poll = get, set, poll
        self.writable = bool(set) if writable is None else writable
        self.choices = tuple(choices) if choices else None
        if self.dtype == "enum" and not self.choices:
            raise ValueError("enum Param needs choices")
        if self.dtype == "enum" and len(self.choices) > 16:  # EPICS ENUM limit - use a number instead
            raise ValueError(f"enum Param with {len(self.choices)} choices: EPICS allows 16")
        if self.dtype == "enum" and any(len(str(c)) > 25 for c in self.choices):
            raise ValueError("enum choices are limited to 25 characters")
        self.limits, self.nelm, self.max_length = limits, nelm, max_length
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        self.kind, self.initial, self.positioner = kind, initial, positioner
        self.overview, self.trend, self.label = overview, trend, label
        self.name = ""

    @property
    def has_rbv(self) -> bool:
        return self.writable and self.get is not None

    def default(self):
        if self.initial is not None:
            return self.initial
        if self.dtype == "enum":
            return self.choices[0]
        return {float: 0.0, int: 0, bool: 0, str: "", "array": [0.0] * max(self.nelm, 1)}[self.dtype]

    def pvprop(self, name: str, read_only: bool, record: bool = False) -> pvproperty:
        # limits are enforced in _InstrumentIOC._put, not as caproto ctrl limits: a put caproto refuses
        # never reaches LastError, and CA clients don't see the refusal
        kw: dict[str, Any] = {"name": name, "read_only": read_only, "doc": self.doc or None}
        units = self.units[:8]  # EPICS EGU holds 8 characters (longer -> "bytes too long" on every read)
        d = self.dtype
        if d is float:
            kw.update(value=float(self.default()), precision=self.precision, units=units)
        elif d is int:
            kw.update(value=int(self.default()), dtype=int, units=units)
        elif d is bool:
            kw.update(value=int(bool(self.default())), dtype=bool)
        elif d is str:
            kw.update(value=str(self.default()), dtype=str, max_length=self.max_length)
        elif d == "enum":
            kw.update(value=self.default(), dtype=ChannelType.ENUM, enum_strings=self.choices)
        elif d == "array":
            kw.update(value=list(self.default()), dtype=float, max_length=self.nelm, units=units,
                      precision=self.precision)
        else:
            raise ValueError(f"unsupported dtype {d!r}")
        if record:  # the leader of a poll group: its .SCAN field sets the group's rate
            kw["record"] = {float: "ai", int: "longin", bool: "bi", "enum": "mbbi"}.get(d, "waveform")
        return pvproperty(**kw)

    def schema(self) -> dict:
        d = self.dtype
        return {k: v for k, v in {
            "name": self.name, "attr": snake(self.name),
            "dtype": d if isinstance(d, str) else d.__name__,
            "units": self.units or None, "precision": self.precision if d in (float, "array") else None,
            "doc": self.doc or None, "writable": self.writable, "rbv": self.has_rbv,
            "readable": self.get is not None or not self.writable,
            "choices": list(self.choices) if self.choices else None,
            "limits": list(self.limits) if self.limits else None,
            "nelm": self.nelm if d == "array" else None, "kind": self.kind, "poll": self.poll,
            "positioner": self.positioner, "overview": self.overview or None, "trend": self.trend or None,
            "label": self.label, "safety": self.safety if self.writable else None, "event": self.event,
        }.items() if v is not None}


class Action:
    """A command. arg: None (put 1 to run), float, int or str (the put value is the argument).
    safety: "safe" | "confirm" (default) | "protected", as for Param.
    exclusive=True: refused (LastError) while another exclusive action runs, instead of
    queueing behind it. lock=False: runs without the instrument lock and without
    Busy/refresh - for commands that must reach a running exclusive action."""

    def __init__(self, func: str | Callable, *, arg: Any = None, doc: str = "", safety: str = "confirm",
                 label: str | None = None, refresh: list[str] | None = None, timeout_s: float | None = None,
                 exclusive: bool = False, lock: bool = True) -> None:
        if safety not in SAFETY:
            raise ValueError(f"safety must be one of {SAFETY}")
        self.func, self.arg, self.doc, self.safety, self.label = func, arg, doc, safety, label
        self.refresh = refresh      # params read right after the action (None = every polled param)
        self.timeout_s = timeout_s  # hint for clients: how long a put may take
        self.exclusive, self.lock = exclusive, lock
        self.name = ""

    def pvprop(self) -> pvproperty:
        if self.arg is None:
            return pvproperty(name=self.name, value=0, dtype=int, doc=self.doc or None)
        if self.arg is str:
            return pvproperty(name=self.name, value="", dtype=str, max_length=1024, doc=self.doc or None)
        return pvproperty(name=self.name, value=self.arg(0), dtype=self.arg, doc=self.doc or None)

    def schema(self) -> dict:
        return {k: v for k, v in {"name": self.name, "attr": snake(self.name),
                                  "arg": None if self.arg is None else self.arg.__name__,
                                  "doc": self.doc or None, "safety": self.safety,
                                  "label": self.label, "timeout_s": self.timeout_s,
                                  "exclusive": self.exclusive or None}.items() if v is not None}


class Instrument:
    """Base class of a declarative instrument. Subclasses declare Params and
    Actions as class attributes and implement open() (+ the getters/setters).
    self.drv is whatever open() connects; with --mock, open() should build the
    simulator (self.mock is True)."""

    TYPE = "generic"
    DESCRIPTION = ""
    DEFAULT_PREFIX = "FASTQ:DEV1:"
    POLL = 1.0                 # default period of params with a getter but no poll given
    ophyd = None               # optional: {"trigger": {...}} hints for the generic ophyd class

    def __init__(self, args) -> None:
        self.args = args
        self.mock = bool(getattr(args, "mock", False))
        self.drv: Any = None
        self._publisher: Callable | None = None  # set by the IOC
        # current values of the params the IOC keeps (writable, no setter): run parameters etc.
        self.values = {n: p.default() for n, p in self.params().items() if p.writable and p.set is None}

    @classmethod
    def add_arguments(cls, parser) -> None:
        """Instrument-specific command-line options (address, port, ...)."""

    def open(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        close = getattr(self.drv, "close", None)
        if callable(close):
            close()

    @classmethod
    def params(cls) -> dict[str, Param]:
        out = {}
        for klass in reversed(cls.__mro__):
            for k, v in vars(klass).items():
                if isinstance(v, Param):
                    v.name = k
                    out[k] = v
        return out

    @classmethod
    def actions(cls) -> dict[str, Action]:
        out = {}
        for klass in reversed(cls.__mro__):
            for k, v in vars(klass).items():
                if isinstance(v, Action):
                    v.name = k
                    out[k] = v
        return out

    @classmethod
    def schema(cls) -> dict:
        return {"type": cls.TYPE, "description": cls.DESCRIPTION.strip(),
                "params": [p.schema() for p in cls.params().values()],
                "actions": [a.schema() for a in cls.actions().values()],
                "ophyd": cls.ophyd or {}}

    def instance_schema(self) -> dict:
        """The schema this IOC publishes; override to adapt it to the command line
        (e.g. a positioner tolerance in the configured units)."""
        return self.schema()

    def call(self, ref: str | Callable, *args):
        return ref(self, *args) if callable(ref) else getattr(self, ref)(*args)

    def publish(self, **values) -> None:
        """Update read-only params from driver code running in a worker thread (getters,
        actions): e.g. the progress and partial data of a long run. Blocks until written."""
        if self._publisher is not None:
            self._publisher(values)

    def on_exit(self) -> None:
        """IOC shutting down (also killed mid-action): leave the instrument safe."""


def _coerce(p: Param, value):
    if p.dtype is float:
        return float(value)
    if p.dtype is int:
        return int(value)
    if p.dtype is bool:
        return pv_bool(value)
    if p.dtype == "enum":
        return enum_choice(value, p.choices)
    if p.dtype is str:
        return value.decode() if isinstance(value, bytes) else str(value)
    return np.asarray(value, dtype=float)


def _to_pv(p: Param, value):
    """driver value -> what instance.write accepts."""
    if value is None:
        return None
    if p.dtype is bool:
        return int(bool(value))
    if p.dtype == "enum":
        return value if isinstance(value, str) else p.choices[int(value)]
    if p.dtype == "array":
        arr = np.asarray(value, dtype=float).ravel()[: p.nelm]
        return arr
    if p.dtype is str:
        return str(value)[: p.max_length - 1]
    return p.dtype(value)


def _poll_groups(icls: type[Instrument]) -> tuple[dict[float, list[Param]], list[Param]]:
    groups: dict[float, list[Param]] = {}
    once: list[Param] = []
    for p in icls.params().values():
        if p.get is None:
            continue
        period = icls.POLL if p.poll is None else p.poll
        if period:
            groups.setdefault(float(period), []).append(p)
        else:
            once.append(p)
    return groups, once


def _poll_leaders(icls: type[Instrument]) -> dict[str, float]:
    groups, _ = _poll_groups(icls)
    return {(m[0].name + "_RBV" if m[0].has_rbv else m[0].name): period for period, m in sorted(groups.items())}


def build_ioc_class(icls: type[Instrument], schema: dict | None = None) -> type:
    """The caproto PVGroup class for an Instrument class."""
    params, actions = icls.params(), icls.actions()
    attrs: dict[str, Any] = {"IOC_TYPE": icls.TYPE}
    schema = dict(schema or icls.schema())
    schema["poll_leaders"] = _poll_leaders(icls)  # PVs whose .SCAN sets a poll group's rate
    schema_text = json.dumps(schema)
    attrs["Schema"] = pvproperty(name="IOC:SCHEMA", value=schema_text, dtype=str,
                                 max_length=max(len(schema_text) + 1, 1024), read_only=True)

    # poll groups: period -> [params]; the first param of each group leads (its .SCAN = group rate)
    groups, once = _poll_groups(icls)

    readback_attr = {}  # param name -> attribute holding its readback
    for name, p in params.items():
        if p.has_rbv:
            attrs[name] = p.pvprop(name, read_only=False)
            attrs[name + "_RBV"] = p.pvprop(name + "_RBV", read_only=True)
            readback_attr[name] = name + "_RBV"
        else:
            attrs[name] = p.pvprop(name, read_only=not p.writable)
            readback_attr[name] = name
    leaders = {}
    for period, members in groups.items():
        lead = readback_attr[members[0].name]
        attrs[lead] = members[0].pvprop(lead, read_only=lead != members[0].name or not members[0].writable,
                                        record=True)
        leaders[lead] = (period, members)
    for name, a in actions.items():
        attrs[name] = a.pvprop()

    cls = type(f"{icls.__name__}IOC", (_InstrumentIOC,), attrs)
    cls._instrument_class = icls
    cls._params, cls._actions, cls._readback_attr = params, actions, readback_attr
    cls._poll_once = once
    cls._leaders = leaders

    for lead, (period, members) in leaders.items():
        def make_scan(members=members):
            async def scan(group, instance, async_lib):
                await group._poll(members)
            return scan
        getattr(cls, lead).scan(period=period, use_scan_field=True)(make_scan())
        # pvproperty.scan registered on the class attribute; caproto builds it per instance

    for name, p in params.items():
        if p.writable:
            def make_put(p=p):
                async def put(group, instance, value):
                    return await group._put(p, value)
                return put
            getattr(cls, name).putter(make_put())
    for name, a in actions.items():
        def make_act(a=a):
            async def act(group, instance, value):
                return await group._act(a, value)
            return act
        getattr(cls, name).putter(make_act())
    return cls


class _InstrumentIOC(FastPollGroup):
    _instrument_class: type[Instrument]
    _params: dict
    _actions: dict
    _readback_attr: dict
    _poll_once: list
    _leaders: dict

    LastError = pvproperty(value="", dtype=str, max_length=1024, read_only=True)
    ErrorCount = pvproperty(value=0, dtype=int, read_only=True)
    Busy = pvproperty(value=0, dtype=bool, read_only=True)

    @Busy.startup
    async def Busy(self, instance, async_lib):
        self._loop = asyncio.get_running_loop()
        # connect right away; polled groups retry by themselves, an IOC without them retries here
        while not await self._ensure_connected() and not self._leaders:
            await asyncio.sleep(RECONNECT_S)

    def __init__(self, instrument: Instrument, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.inst = instrument
        self._lock = asyncio.Lock()
        self._connected = False
        self._last_connect_try = 0.0
        self._seeded: set[str] = set()
        self._exclusive_running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        instrument._publisher = self._publish_threadsafe
        self._init_fast_poll({lead: period for lead, (period, _) in self._leaders.items()})

    async def _io(self, func, *args):
        return await run_blocking(func, *args, lock=self._lock)

    async def _error(self, where: str, exc: BaseException) -> None:
        msg = f"{time.strftime('%H:%M:%S')} {where}: {type(exc).__name__}: {exc}"
        log.warning(msg)
        log.debug("%s", traceback.format_exc())
        await self.LastError.write(msg[:1000])
        await self.ErrorCount.write(int(self.ErrorCount.value) + 1)

    async def _ensure_connected(self) -> bool:
        if self._connected:
            return True
        now = time.monotonic()
        if now - self._last_connect_try < RECONNECT_S and self._last_connect_try:
            return False
        self._last_connect_try = now
        try:
            await self._io(self.inst.open)
        except Exception as exc:
            await self._error("open", exc)
            await self.Connected.write(0)
            return False
        self._connected = True
        log.info("%s connected", self.inst.TYPE)
        await self.Connected.write(1)
        await self._poll(self._poll_once)
        for _, members in self._leaders.values():  # slow groups: values now, not after one period
            await self._poll(members)
        return True

    async def _lost(self, where: str, exc: BaseException) -> None:
        await self._error(where, exc)
        if isinstance(exc, (ConnectionError, OSError, TimeoutError)):
            self._connected = False
            await self.Connected.write(0)
            try:
                await self._io(self.inst.close)
            except Exception:
                pass

    async def _write_value(self, p: Param, value) -> None:
        v = _to_pv(p, value)
        if v is None:
            return
        attr = self._readback_attr[p.name]
        await getattr(self, attr).write(v, status=AlarmStatus.NO_ALARM, severity=AlarmSeverity.NO_ALARM)
        if p.has_rbv and p.name not in self._seeded:
            # show the instrument's value in the setpoint PV too - display only, no putter
            self._seeded.add(p.name)
            await getattr(self, p.name).write(v, verify_value=False)

    def _publish_threadsafe(self, values: dict) -> None:
        for name in values:
            if self._params[name].writable:
                raise ValueError(f"publish: {name} is writable - only read-only params can be published")
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._publish_values(values), self._loop).result(30)

    async def _publish_values(self, values: dict) -> None:
        for name, value in values.items():
            try:
                await self._write_value(self._params[name], value)
            except Exception as exc:
                await self._error(f"publish {name}", exc)

    async def _poll(self, members) -> None:
        if not await self._ensure_connected():
            return
        for p in members:
            try:
                value = await self._io(self.inst.call, p.get)
            except Exception as exc:
                await self._lost(f"read {p.name}", exc)
                await getattr(self, self._readback_attr[p.name]).alarm.write(
                    status=AlarmStatus.READ, severity=AlarmSeverity.INVALID_ALARM)
                if not self._connected:
                    return
                continue
            try:
                await self._write_value(p, value)
            except Exception as exc:
                await self._error(f"publish {p.name}={value!r}", exc)

    async def _check_limits(self, p: Param, v) -> None:
        if p.limits and p.dtype in (float, int) and not (p.limits[0] <= v <= p.limits[1]):
            exc = ValueError(f"{p.name}={v} outside limits {p.limits}")
            await self._error("put", exc)
            raise exc

    async def _put(self, p: Param, value):
        await self._refuse_if_read_only(f"{p.name}={value!r}")
        if p.set is None:  # a value the IOC keeps (e.g. a run parameter)
            v = _coerce(p, value)
            await self._check_limits(p, v)
            self.inst.values[p.name] = v
            if p.refresh and self._connected:
                await self._poll([self._params[n] for n in p.refresh])
            return value
        if not await self._ensure_connected():
            raise ConnectionError(f"{self.inst.TYPE} not connected")
        v = _coerce(p, value)
        await self._check_limits(p, v)
        await self.Busy.write(1)
        try:
            await self._io(self.inst.call, p.set, v)
        except Exception as exc:
            await self._lost(f"put {p.name}={v!r}", exc)
            raise
        finally:
            await self.Busy.write(0)
        if p.get is not None:  # fresh readback right away
            try:
                await self._write_value(p, await self._io(self.inst.call, p.get))
            except Exception as exc:
                await self._error(f"read {p.name} after put", exc)
        if p.refresh:
            await self._poll([self._params[n] for n in p.refresh])
        return value

    async def _refuse_if_read_only(self, what: str) -> None:
        if getattr(self.inst.args, "read_only", False):
            exc = PermissionError(f"{what} refused: IOC runs --read-only")
            await self._error("put", exc)
            raise exc

    async def _act(self, a: Action, value):
        if a.arg is None and not pv_bool(value):
            return 0
        await self._refuse_if_read_only(f"action {a.name}")
        args = () if a.arg is None else (a.arg(value.decode() if isinstance(value, bytes) else value),)
        if not a.lock:  # reaches a running exclusive action; no Busy, no refresh
            try:
                log.info("action %s%s (unlocked)", a.name, args)
                await run_blocking(self.inst.call, a.func, *args)
            except Exception as exc:
                await self._error(f"action {a.name}", exc)
                raise
            return 0 if a.arg is None else value
        if a.exclusive and self._exclusive_running:
            exc = RuntimeError(f"{a.name} refused: an exclusive action is still running")
            await self._error(f"action {a.name}", exc)
            raise exc
        if not await self._ensure_connected():
            raise ConnectionError(f"{self.inst.TYPE} not connected")
        self._exclusive_running |= a.exclusive
        await self.Busy.write(1)
        try:
            log.info("action %s%s", a.name, args)
            await self._io(self.inst.call, a.func, *args)
        except Exception as exc:
            await self._lost(f"action {a.name}", exc)
            raise
        finally:
            if a.exclusive:
                self._exclusive_running = False
            await self.Busy.write(0)
        # actions change state: publish the fresh values before the put completes
        if a.refresh is not None:
            await self._poll([self._params[n] for n in a.refresh])
        else:
            for _, members in self._leaders.values():
                await self._poll(members)
            await self._poll(self._poll_once)  # settings read only on demand may have changed too
        return 0 if a.arg is None else value


def _without_also(argv: list[str]) -> list[str]:
    out, skip = [], False
    for tok in argv:
        if skip:
            skip = False
        elif tok == "--also":
            skip = True
        elif not tok.startswith("--also="):
            out.append(tok)
    return out


def run_instrument(icls: type[Instrument], argv: list[str] | None = None) -> None:
    """Command-line entry point of an instrument IOC:  python <type>_ioc.py --prefix ... [--mock]

    --also "<options>" (repeatable) serves one more instrument of the same type from this
    process, for devices whose vendor library must be loaded once for all of them (e.g. two
    cameras on one SDK installation). Its options are added to the main command line (later
    ones win), so usually only --prefix and the unit selection differ. Each instrument keeps
    its own lock, connection and PV prefix."""
    import shlex
    import sys
    from types import SimpleNamespace

    parser, split_args = ioc_arg_parser(icls.DEFAULT_PREFIX, icls.DESCRIPTION or icls.TYPE)
    parser.add_argument("--mock", action="store_true", help="simulated instrument")
    parser.add_argument("--print-schema", action="store_true",
                        help="print the schema this IOC would publish (with these options) and exit")
    parser.add_argument("--also", action="append", default=[], metavar="OPTIONS",
                        help='serve another instrument of this type from this process, e.g. '
                             '--also "--prefix LAB:CAM2: --camera-index 2"')
    icls.add_arguments(parser)
    if not any(a.dest == "read_only" for a in parser._actions):  # an instrument may define its own
        parser.add_argument("--read-only", action="store_true",
                            help="monitoring only: every put of a setting and every action is refused")
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)
    if args.print_schema:
        try:
            schema = icls(args).instance_schema()
        except Exception:  # noqa: BLE001 - options this check cannot satisfy: the class schema
            schema = icls.schema()
        print(json.dumps(schema, indent=1))
        return
    ioc_options, run_options = split_args(args)
    setup_logging()
    insts = [icls(args)]
    groups = [build_ioc_class(icls, insts[0].instance_schema())(instrument=insts[0], **ioc_options)]
    base = _without_also(argv)
    for extra in args.also:
        # posix=False keeps Windows paths intact; strip the quotes it leaves on tokens
        tokens = [t.strip("'\"") for t in shlex.split(extra, posix=False)]
        a = parser.parse_args(base + tokens)
        inst = icls(a)
        insts.append(inst)
        groups.append(build_ioc_class(icls, inst.instance_schema())(instrument=inst, **split_args(a)[0]))
    pvdb: dict = {}
    for g in groups:
        dup = set(pvdb) & set(g.pvdb)
        if dup:
            raise SystemExit(f"--also: PV names served twice (same --prefix?): {sorted(dup)[:3]}")
        pvdb.update(g.pvdb)
    if len(groups) > 1:
        log.info("serving %d %s instruments: %s", len(groups), icls.TYPE, [g.prefix for g in groups])
    try:
        serve(groups[0] if len(groups) == 1 else SimpleNamespace(pvdb=pvdb), args, run_options)
    finally:
        for inst in insts:
            try:
                inst.on_exit()
            except Exception:
                log.exception("on_exit failed - CHECK THE INSTRUMENT STATE")
