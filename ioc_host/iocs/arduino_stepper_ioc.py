"""IOC: Arduino AccelStepper controller, up to 3 stepper motors on one serial port
(simpleRPC firmware 4dmag arduino/stepper, 9600 baud; motors addressed by index 0..2).

    python ioc_host/iocs/arduino_stepper_ioc.py --port COM28 --axes "Lens:mm,ND:deg,Iris:a.u." \\
        --limit-switches 0,1,1 --max-speed 600 --accel 800 --min-pulse-width 200 --prefix FDM:STEPPERS:
    python ioc_host/iocs/arduino_stepper_ioc.py --mock --prefix TEST:STEPPERS:

--axes "Name:unit[:steps_per_unit],..." in motor order. Per axis: <Name> (position,
a positioner), <Name>Moving, <Name>Velocity / <Name>Acceleration (steps/s, steps/s2),
<Name>AtLimit, <Name>Homed, <Name>LimitSwitch, <Name>JogStep and the actions
<Name>MoveRel (setPosRelative), <Name>JogForward / <Name>JogReverse, <Name>Home / <Name>Stop.
The firmware (stepper.ino) has no getters for limit-switch use, minimum pulse width
or the timeout, and no "set position" (Home without a limit switch zeroes instead).

Opening the port resets the Arduino, which then forgets speed, acceleration and
limit-switch use (firmware defaults: 1 step/s, all limit switches on). 4dmag
therefore wrote them on every connect; --limit-switches/--max-speed/--accel/
--min-pulse-width do the same here (only the options given are written).
With a limit switch in use the firmware does not move a motor before it was
homed - a move is refused here instead of hanging.
"""
from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402

DEFAULT_AXES = "Lens:mm,ND:deg,Iris:a.u."
FW_MOTORS = 3


class RPC:
    """simpleRPC over serial with a read timeout (the library default blocks forever)."""

    def __init__(self, port: str, baud: int, timeout: float = 3.0, reset: bool = True) -> None:
        from simple_rpc import SerialInterface

        self.h = SerialInterface(port, baudrate=baud, autoconnect=False)
        self.h._connection.timeout = self.timeout = timeout
        if not reset:  # DTR low while opening: the Arduino keeps running (no auto-reset)
            self.h._connection.dtr = False
        try:
            self.h.open()
        except struct.error as exc:
            self.h.close()
            raise TimeoutError(f"no simpleRPC answer on {port}") from exc

    def call(self, name, *args, timeout=None):
        self.h._connection.timeout = timeout or self.timeout
        try:
            return self.h.call_method(name, *args)
        except struct.error as exc:  # short read = timeout; the byte stream is out of step now
            raise TimeoutError(f"{name}: no reply") from exc
        finally:
            self.h._connection.timeout = self.timeout

    def close(self):
        self.h.close()


class SimMotor:
    def __init__(self) -> None:
        self.pos, self.target, self.speed, self.accel = 0.0, 0, 600.0, 800.0
        self.homed, self.limit_switch, self.t = False, True, time.monotonic()

    def update(self):
        now = time.monotonic()
        if self.limit_switch and not self.homed:  # firmware loop(): no steps before homing
            self.t = now
            return
        step = self.speed * (now - self.t)
        self.t = now
        d = self.target - self.pos
        self.pos = self.target if abs(d) <= step else self.pos + step * (1 if d > 0 else -1)


class SimRPC:
    """The firmware's RPC methods on simulated motors (no acceleration ramp)."""

    def __init__(self) -> None:
        self.m = [SimMotor() for _ in range(FW_MOTORS)]
        self.timeout = 120

    def call(self, name, *args, timeout=None):
        m = self.m[args[0]] if args and name != "setTimeout" else None
        if m:
            m.update()
        if name == "setTimeout":
            self.timeout = args[0]
        elif name == "setLimitSwitches":
            m.limit_switch = bool(args[1])
            return m.limit_switch
        elif name in ("setPos", "setPosRelative"):
            m.target = int(args[1]) + (round(m.pos) if name == "setPosRelative" else 0)
            return -1
        elif name == "getCurrentPosition":
            return int(round(m.pos))
        elif name == "isRunning":
            return round(m.pos) != m.target
        elif name == "stop":
            m.target = int(round(m.pos))
        elif name == "home":
            if m.limit_switch:
                time.sleep(abs(m.pos) / max(m.speed, 1.0) / 10)
                m.homed = True
            m.pos = m.target = 0
            return True
        elif name == "isHomed":
            return m.homed
        elif name == "isAtLimit":
            return m.limit_switch and m.homed and round(m.pos) == 0
        elif name == "getMaxSpeed":
            return m.speed
        elif name == "setMaxSpeed":
            m.speed = float(args[1])
        elif name == "getAcceleration":
            return m.accel
        elif name == "setAcceleration":
            m.accel = float(args[1])
        elif name == "setMinPulseWidth":
            pass
        else:
            raise ValueError(f"no RPC method {name}")
        return None


def parse_axes(text: str) -> list[tuple[str, str, float]]:
    axes = []
    for item in filter(None, (s.strip() for s in text.split(","))):
        name, unit, *rest = item.split(":") + [""]
        axes.append((name, unit or "steps", float(rest[0]) if rest and rest[0] else 1.0))
    if not 1 <= len(axes) <= FW_MOTORS:
        raise ValueError(f"--axes: 1..{FW_MOTORS} motors")
    return axes


def int_list(text: str | None, n: int, cast=int):
    """'1,0,1' or one value for all motors."""
    if text is None:
        return None
    vals = [cast(v) for v in str(text).split(",")]
    return vals * n if len(vals) == 1 else vals


class ArduinoStepper(Instrument):
    TYPE = "arduino_stepper"
    DESCRIPTION = "Arduino stepper-motor controller (AccelStepper, up to 3 motors: HHG lens, pump ND filter, HHG iris)"
    DEFAULT_PREFIX = "FDM:STEPPERS:"
    POLL = 1.0
    AXES: list = []

    ophyd = {"stop": "Stop"}

    # firmware: timeout = t * 1000 in 16-bit int arithmetic -> only 1..32 s work; its own default
    # (120 * 1000, overflowed) means "never" - homing without a reachable switch then blocks the board
    MotionTimeoutS = Param(int, units="s", limits=(1, 32), writable=True, initial=120,
                           set="set_motion_timeout", kind="config", safety="safe",
                           doc="firmware timeout of homing and blocking moves, 1..32 s (shows the last value "
                               "written; 120 = firmware default, which in fact never times out)")
    Stop = Action("stop_all", safety="safe", doc="stop all motors")

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--port", default="COM28")
        parser.add_argument("--baud", type=int, default=9600)
        parser.add_argument("--axes", default=DEFAULT_AXES, help='"Name:unit[:steps_per_unit],..." in motor order')
        parser.add_argument("--limit-switches", help="written on connect: 1/0 per motor, e.g. 0,1,1")
        parser.add_argument("--max-speed", help="written on connect: steps/s, one value or one per motor")
        parser.add_argument("--accel", help="written on connect: steps/s^2, one value or one per motor")
        parser.add_argument("--min-pulse-width", help="written on connect: step pulse width in us")
        parser.add_argument("--no-reset", action="store_true",
                            help="open the port with DTR low so the Arduino is not reset (keeps its configuration)")

    def __init__(self, args) -> None:
        super().__init__(args)
        # what we know about the limit-switch use (no getter in the firmware): firmware default on
        self.limit = limit_switches(args.limit_switches, len(self.AXES))
        self.motion_timeout = 120

    def open(self) -> None:
        a = self.args
        self.drv = SimRPC() if self.mock else RPC(a.port, a.baud, reset=not a.no_reset)
        if not a.no_reset:
            self.motion_timeout = 120  # the reset board is back at its default
        n = len(self.AXES)
        # restore the 4dmag start-up configuration (lost at every Arduino reset); only what was given
        for opt, method, cast in ((a.limit_switches, "setLimitSwitches", lambda v: bool(int(v))),
                                  (a.max_speed, "setMaxSpeed", float), (a.accel, "setAcceleration", float),
                                  (a.min_pulse_width, "setMinPulseWidth", int)):
            for i, v in enumerate(int_list(opt, n, cast) or []):
                self.drv.call(method, i, v)

    def move(self, i: int, value: float) -> None:
        if self.limit[i] and not self.drv.call("isHomed", i):
            raise RuntimeError(f"{self.AXES[i][0]}: not homed - the firmware does not move it before Home "
                               "(limit switch in use)")
        self.drv.call("setPos", i, int(round(value * self.AXES[i][2])), False)

    def move_rel(self, i: int, delta: float) -> None:
        if self.limit[i] and not self.drv.call("isHomed", i):
            raise RuntimeError(f"{self.AXES[i][0]}: not homed - the firmware does not move it before Home "
                               "(limit switch in use)")
        self.drv.call("setPosRelative", i, int(round(delta * self.AXES[i][2])), False)

    def home(self, i: int) -> None:
        # the firmware blocks (all motors stand still) until the switch is found or its timeout ends
        if not self.drv.call("home", i, timeout=min(self.motion_timeout, 600) + 10) and self.limit[i]:
            raise RuntimeError(f"{self.AXES[i][0]}: homing failed (limit switch not reached within the timeout)")

    def set_motion_timeout(self, value: int) -> None:
        self.drv.call("setTimeout", value)
        self.motion_timeout = value

    def set_limit_switch(self, i: int, value: bool) -> None:
        self.drv.call("setLimitSwitches", i, bool(value))
        self.limit[i] = int(bool(value))

    def stop_all(self) -> None:
        for i in range(len(self.AXES)):
            self.drv.call("stop", i)


def limit_switches(text: str | None, n: int) -> list[int]:
    return int_list(text, n) or [1] * n


def make_class(axes: list[tuple[str, str, float]], limits: list[int] | None = None) -> type[ArduinoStepper]:
    """ArduinoStepper with one set of params per motor (names from --axes)."""
    attrs: dict = {"AXES": axes}
    limits = limits or [1] * len(axes)
    for i, (name, unit, spu) in enumerate(axes):
        attrs[name] = Param(float, units=unit, precision=3, poll=0.2, kind="hinted", overview=True, trend=True,
                            get=lambda s, i=i, spu=spu: s.drv.call("getCurrentPosition", i) / spu,
                            set=lambda s, v, i=i: s.move(i, v),
                            positioner={"readback": name, "moving": name + "Moving", "tolerance": 0.5 / spu,
                                        "settle_s": 0.0, "timeout_s": 300.0},
                            doc=f"motor {i} position ({spu:g} steps per {unit})")
        attrs[name + "Moving"] = Param(bool, poll=0.2, kind="omitted", get=lambda s, i=i: s.drv.call("isRunning", i))
        attrs[name + "Velocity"] = Param(float, units="steps/s", precision=1, limits=(1, 4000), poll=0, kind="config",
                                      get=lambda s, i=i: s.drv.call("getMaxSpeed", i), safety="safe",
                                      set=lambda s, v, i=i: s.drv.call("setMaxSpeed", i, v), doc="max speed")
        attrs[name + "Acceleration"] = Param(float, units="steps/s2", precision=1, limits=(1, 100000), poll=0,
                                      kind="config", get=lambda s, i=i: s.drv.call("getAcceleration", i),
                                      safety="safe", set=lambda s, v, i=i: s.drv.call("setAcceleration", i, v),
                                      doc="acceleration")
        attrs[name + "JogStep"] = Param(float, units=unit, precision=3, writable=True, initial=1.0 / spu * 100,
                                        limits=(0.0, 1e9), kind="config", safety="safe",
                                        doc=f"step of {name}JogForward / {name}JogReverse")
        attrs[name + "MoveRel"] = Action(lambda s, v, i=i: s.move_rel(i, v), arg=float,
                                         doc=f"move by this many {unit} (setPosRelative)")
        attrs[name + "JogForward"] = Action(lambda s, i=i, n=name: s.move_rel(i, s.values[n + "JogStep"]),
                                            doc="move +JogStep")
        attrs[name + "JogReverse"] = Action(lambda s, i=i, n=name: s.move_rel(i, -s.values[n + "JogStep"]),
                                            doc="move -JogStep")
        attrs[name + "AtLimit"] = Param(bool, poll=1.0, kind="config", get=lambda s, i=i: s.drv.call("isAtLimit", i),
                                        doc="limit switch closed")
        attrs[name + "Homed"] = Param(bool, poll=2.0, kind="config", get=lambda s, i=i: s.drv.call("isHomed", i))
        # off = moves without homing and no end-stop pull-back -> protected
        attrs[name + "LimitSwitch"] = Param(bool, writable=True, initial=limits[i], safety="protected",
                                            set=lambda s, v, i=i: s.set_limit_switch(i, v),
                                            kind="config", doc="use the limit switch (homing, end-stop pull-back); "
                                            "shows what was last written - the firmware cannot report it")
        # with a limit switch homing drives (blind, up to 1e8 steps) until the switch closes: a broken
        # switch crashes the mount -> protected; without one it only zeroes the position
        attrs[name + "Home"] = Action(lambda s, i=i: s.home(i), safety="protected" if limits[i] else "confirm",
                                      timeout_s=130.0,
                                      doc="home: drive to the limit switch and zero there (without a limit "
                                          "switch: just zero the current position); blocks the controller")
        attrs[name + "Stop"] = Action(lambda s, i=i: s.drv.call("stop", i), safety="safe", doc="stop (decelerate)",
                                      refresh=[name, name + "Moving"])
    return type("ArduinoStepperAxes", (ArduinoStepper,), attrs)


def class_from_argv(argv=None) -> type[ArduinoStepper]:
    """The params depend on --axes, so they are built before run_instrument parses the command line."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--axes", default=DEFAULT_AXES)
    pre.add_argument("--limit-switches")
    a = pre.parse_known_args(argv)[0]
    axes = parse_axes(a.axes)
    return make_class(axes, limit_switches(a.limit_switches, len(axes)))


if __name__ == "__main__":
    run_instrument(class_from_argv())
