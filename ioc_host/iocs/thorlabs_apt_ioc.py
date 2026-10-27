"""IOC: Thorlabs APT motor (delay line, motorised rotation mount) via APT.dll
(thorlabs_apt Python package - Windows; the 4dmag copy works: --apt-path <folder with thorlabs_apt>).

    python ioc_host/iocs/thorlabs_apt_ioc.py --serial 83000001 --unit ps --t0 75.123 --direction -1 --prefix FDM:DELAY:
    python ioc_host/iocs/thorlabs_apt_ioc.py --serial 27000002 --unit deg --prefix FDM:WAVEPLATE:
    python ioc_host/iocs/thorlabs_apt_ioc.py --mock --unit ps --t0 50 --prefix TEST:DELAY:

Position is in user units: mm / deg as the stage reports, or ps for a delay
line (--unit ps: double pass, t = direction * (x - t0) * 2 / c). The move is
started by the put and the ophyd positioner waits for Moving = 0 and the
readback (set-and-confirm). MoveRel / JogForward / JogReverse move relative to the
current position (user units). Moves outside the stage travel (MOT_GetStageAxisInfo,
linear stages) are refused here instead of running into the end switch.

Home only on explicit request: it starts the homing move and returns; Moving = 1
until the stage has found its home switch (Homed = 1), Stop aborts it. APT homing
drives to a limit switch of the stage itself, i.e. within its own travel.

Everything is APT.dll as wrapped by thorlabs_apt core.py (MOT_* calls): status bits,
velocity / acceleration (+ their limits), home parameters, backlash, stage axis info,
channel enable/disable, identify. Not exposed: PID / DC-servo loop parameters
(stage-tuned in the APT config; writing them wrongly makes the servo oscillate),
continuous velocity moves (MOT_MoveVelocity - a jog by steps is safer).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402

C_MM_PER_PS = 0.299792458  # speed of light, mm / ps
# MOT_GetStatusBits (thorlabs_apt core.py)
FWD_LIMIT, REV_LIMIT, MOVING_MASK, HOMING, HOMED = 0x1, 0x2, 0x10 | 0x20 | 0x40 | 0x80 | 0x200, 0x200, 0x400
TRACKING, SETTLED, MOTION_ERROR, CURRENT_LIMIT, ENABLED = 0x1000, 0x2000, 0x4000, 0x01000000, 0x80000000
STAGE_UNITS = {1: "mm", 2: "deg"}


class SimMotor:
    """thorlabs_apt.Motor without hardware: trapezoid-free move at the max velocity, a home switch at 0."""

    def __init__(self, velocity: float = 5.0, unit: str = "mm") -> None:
        self.pos, self.target, self.t_start, self.p_start = 10.0, 10.0, 0.0, 10.0
        self.velocity, self.acceleration = velocity, 10.0
        self.homing, self.homed, self.enabled = False, False, True
        self.home_params = [2, 1, 2.0, 0.0]  # direction rev, rev limit switch, velocity, zero offset
        self.backlash_distance = 0.0
        self.axis = (0.0, 360.0, 2, 1.0) if unit == "deg" else (0.0, 100.0, 1, 1.0)

    def _now(self):
        if self.t_start:
            dist = self.target - self.p_start
            v = self.home_params[2] if self.homing else self.velocity
            done = min(1.0, v * (time.monotonic() - self.t_start) / max(abs(dist), 1e-12))
            self.pos = self.p_start + dist * done
            if done >= 1.0:
                self.t_start = 0.0
                if self.homing:
                    self.homing, self.homed = False, True
                    self.pos = self.target = self.home_params[3]
        return self.pos

    @property
    def position(self):
        return self._now()

    @property
    def _status_bits(self):
        self._now()
        bits = (ENABLED if self.enabled else 0) | (HOMED if self.homed else 0)
        if self.t_start:
            bits |= HOMING if self.homing else 0x10
        else:
            bits |= SETTLED
        lo, hi = self.axis[:2]
        bits |= (REV_LIMIT if self.pos <= lo else 0) | (FWD_LIMIT if self.pos >= hi else 0)
        return bits

    @property
    def is_in_motion(self):
        return bool(self._status_bits & MOVING_MASK)

    @property
    def has_homing_been_completed(self):
        return self.homed

    def move_to(self, value, blocking=False):
        if not self.enabled:
            raise Exception("Setting absolute position failed: channel disabled")
        self._now()
        self.p_start, self.target, self.t_start = self.pos, float(value), time.monotonic()

    def move_by(self, value, blocking=False):
        self.move_to(self._now() + value)

    def move_home(self, blocking=False):
        self.move_to(self.axis[0])
        self.homing = True

    def stop_profiled(self):
        self._now()
        self.target, self.t_start, self.homing = self.pos, 0.0, False

    def get_velocity_parameters(self):
        return 0.0, self.acceleration, self.velocity

    def set_velocity_parameters(self, vmin, acc, vmax):
        self.acceleration, self.velocity = acc, vmax

    def get_velocity_parameter_limits(self):
        return 20.0, 10.0

    def get_move_home_parameters(self):
        return tuple(self.home_params)

    def set_move_home_parameters(self, direction, lim_switch, velocity, zero_offset):
        self.home_params = [direction, lim_switch, velocity, zero_offset]

    def get_stage_axis_info(self):
        return self.axis

    def enable(self):
        self.enabled = True

    def disable(self):
        self.stop_profiled()
        self.enabled = False

    def identify(self):
        pass


class ThorlabsAPT(Instrument):
    TYPE = "thorlabs_apt"
    DESCRIPTION = "Thorlabs APT motor stage (delay line in ps, rotation mount in deg, or plain mm)"
    DEFAULT_PREFIX = "FDM:MOTOR1:"
    POLL = 0.2
    ophyd = {"stop": "Stop"}

    Position = Param(float, precision=4, get="get_position", set="move", poll=0.1, kind="hinted",
                     positioner={"readback": "Position", "moving": "Moving", "tolerance": 0.0, "settle_s": 0.0,
                                 "timeout_s": 300.0},
                     overview=True, trend=True, doc="position in user units (Units)")
    Moving = Param(bool, get=lambda s: s.bits() & MOVING_MASK, poll=0.1, kind="omitted", overview=True,
                   doc="moving or homing")
    RawPosition = Param(float, units="mm", precision=5, get=lambda s: s.drv.position, poll=1.0, kind="config",
                        doc="stage position as reported (mm or deg)")
    Velocity = Param(float, precision=3, limits=(0.0, 100.0), get=lambda s: s.drv.get_velocity_parameters()[2],
                     set="set_velocity", poll=0, kind="config", safety="safe",
                     doc="max velocity (stage units / s; at most MaxVelocity)")
    Acceleration = Param(float, precision=3, limits=(0.0, 1000.0), poll=0, kind="config", safety="safe",
                         get=lambda s: s.drv.get_velocity_parameters()[1], set="set_acceleration",
                         doc="acceleration (stage units / s2; at most MaxAcceleration)")
    MaxVelocity = Param(float, precision=3, poll=0, kind="config",
                        get=lambda s: s.drv.get_velocity_parameter_limits()[1], doc="velocity limit of the motor")
    MaxAcceleration = Param(float, precision=3, poll=0, kind="config",
                            get=lambda s: s.drv.get_velocity_parameter_limits()[0],
                            doc="acceleration limit of the motor")
    Backlash = Param(float, precision=4, limits=(0.0, 5.0), poll=0, kind="config",
                     get=lambda s: s.drv.backlash_distance, set=lambda s, v: setattr(s.drv, "backlash_distance", v),
                     doc="backlash correction distance (stage units)")
    Homed = Param(bool, get=lambda s: s.bits() & HOMED, poll=1.0, kind="config", doc="homing completed")
    FwdLimit = Param(bool, get=lambda s: s.bits() & FWD_LIMIT, poll=1.0, kind="config",
                     doc="forward hardware limit switch active")
    RevLimit = Param(bool, get=lambda s: s.bits() & REV_LIMIT, poll=1.0, kind="config",
                     doc="reverse hardware limit switch active")
    MotionError = Param(bool, get=lambda s: s.bits() & MOTION_ERROR, poll=1.0, overview=True,
                        doc="excessive position error (servo)")
    CurrentLimit = Param(bool, get=lambda s: s.bits() & CURRENT_LIMIT, poll=1.0, kind="config",
                         doc="motor current limit reached")
    Enabled = Param(bool, get=lambda s: s.bits() & ENABLED, poll=1.0, kind="config", doc="motor channel enabled")
    Settled = Param(bool, get=lambda s: s.bits() & SETTLED, poll=1.0, kind="omitted", doc="in position, settled")
    StatusBits = Param(int, get=lambda s: s.bits() & 0xFFFFFFFF, poll=1.0, kind="omitted",
                       doc="MOT_GetStatusBits (all flags)")
    TravelMin = Param(float, precision=4, poll=0, kind="config", get=lambda s: s.axis_info()[0],
                      doc="stage travel minimum (stage units)")
    TravelMax = Param(float, precision=4, poll=0, kind="config", get=lambda s: s.axis_info()[1],
                      doc="stage travel maximum (stage units)")
    StageUnits = Param(str, poll=0, kind="config", get=lambda s: STAGE_UNITS.get(s.axis_info()[2], "?"),
                       doc="units the stage reports in")
    HomeVelocity = Param(float, precision=3, limits=(0.0, 100.0), poll=0, kind="config", safety="safe",
                         get=lambda s: s.drv.get_move_home_parameters()[2], set="set_home_velocity",
                         doc="homing velocity (stage units / s)")
    HomeOffset = Param(float, precision=4, poll=0, kind="config", get=lambda s: s.drv.get_move_home_parameters()[3],
                       doc="home zero offset (stage units)")
    Units = Param(str, get=lambda s: s.args.unit, poll=0, kind="config")
    JogStep = Param(float, precision=4, writable=True, initial=0.1, limits=(0.0, 1e6), kind="config", safety="safe",
                    doc="step of JogForward / JogReverse (user units)")

    MoveRel = Action("move_rel", arg=float, doc="move by this many user units (relative)")
    JogForward = Action(lambda s: s.move_rel(s.values["JogStep"]), doc="move +JogStep")
    JogReverse = Action(lambda s: s.move_rel(-s.values["JogStep"]), doc="move -JogStep")
    # within the stage's own travel, so it cannot hit anything the stage carries -> confirm, not protected
    Home = Action("home", doc="home the stage (drives to its home switch; Moving = 1 until done)",
                  refresh=["Moving", "Homed"])
    Stop = Action("stop", safety="safe", doc="stop the motion (profiled)",
                  refresh=["Position", "Moving"])
    EnableMotor = Action(lambda s: s.drv.enable(), doc="enable the motor channel", refresh=["Enabled"])
    DisableMotor = Action(lambda s: s.drv.disable(), doc="disable the motor channel (no holding torque)",
                          refresh=["Enabled", "Moving"])
    Identify = Action(lambda s: s.drv.identify(), safety="safe", doc="flash the controller's front-panel LED",
                      refresh=[])

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--serial", type=int, help="APT serial number")
        parser.add_argument("--unit", choices=("mm", "deg", "ps"), default="mm")
        parser.add_argument("--t0", type=float, default=0.0, help="ps mode: stage position (mm) of time zero")
        parser.add_argument("--direction", type=int, choices=(1, -1), default=1, help="ps mode: sign")
        parser.add_argument("--offset", type=float, default=0.0, help="mm/deg mode: user = raw - offset")
        parser.add_argument("--apt-path", default="", help="folder that contains the thorlabs_apt package")
        parser.add_argument("--tolerance", type=float, default=0.005,
                            help="a move is done when stopped within this many user units of the target")

    def __init__(self, args) -> None:
        super().__init__(args)
        self._bits, self._bits_t, self._axis = 0, 0.0, None

    def instance_schema(self) -> dict:
        schema = self.schema()
        raw = "deg" if self.args.unit == "deg" else "mm"
        for p in schema["params"]:
            if p["name"] in ("Position", "JogStep"):
                p["units"] = self.args.unit
            elif p["name"] in ("RawPosition", "Backlash", "TravelMin", "TravelMax", "HomeOffset"):
                p["units"] = raw
            elif p["name"] in ("Velocity", "MaxVelocity", "HomeVelocity"):
                p["units"] = raw + "/s"
            elif p["name"] in ("Acceleration", "MaxAcceleration"):
                p["units"] = raw + "/s2"
            if p["name"] == "Position":
                p["positioner"] = {**p["positioner"], "tolerance": self.args.tolerance}
        return schema

    def open(self) -> None:
        self._axis = None
        if self.mock:
            self.drv = SimMotor(unit=self.args.unit)
            return
        if self.args.apt_path:
            sys.path.insert(0, self.args.apt_path)
        import thorlabs_apt as apt

        if self.args.serial is None:
            raise ValueError(f"--serial required; available: {apt.list_available_devices()}")
        self.drv = apt.Motor(self.args.serial)

    def close(self) -> None:
        # nothing to release per motor (APT.dll cleans up at exit). The old close() disabled the
        # channel, so after a lost connection the stage came back disabled and refused every move.
        pass

    def bits(self) -> int:
        """MOT_GetStatusBits once per poll round (all flags of a group are read within ms)."""
        if time.monotonic() - self._bits_t > 0.05:
            self._bits, self._bits_t = int(self.drv._status_bits), time.monotonic()
        return self._bits

    def axis_info(self) -> tuple:
        if self._axis is None:
            self._axis = tuple(self.drv.get_stage_axis_info())
        return self._axis

    # user <-> stage units
    def to_user(self, raw: float) -> float:
        a = self.args
        if a.unit == "ps":
            return a.direction * (raw - a.t0) * 2.0 / C_MM_PER_PS
        return raw - a.offset

    def to_raw(self, user: float) -> float:
        a = self.args
        if a.unit == "ps":
            return a.t0 + a.direction * user * C_MM_PER_PS / 2.0
        return user + a.offset

    def get_position(self) -> float:
        return self.to_user(self.drv.position)

    def _check_travel(self, raw: float) -> None:
        lo, hi, units = self.axis_info()[:3]
        if units == 1 and hi > lo and not lo - 1e-6 <= raw <= hi + 1e-6:  # linear stages only (rotation wraps)
            raise ValueError(f"target {raw:.4f} mm outside the stage travel {lo:g} .. {hi:g} mm")

    def move(self, value: float) -> None:
        raw = self.to_raw(value)
        self._check_travel(raw)
        self._bits_t = 0.0
        self.drv.move_to(raw, blocking=False)

    def move_rel(self, delta: float) -> None:
        self.move(self.get_position() + delta)

    def home(self) -> None:
        self._bits_t = 0.0
        self.drv.move_home(blocking=False)

    def stop(self) -> None:
        self.drv.stop_profiled()
        self._bits_t = 0.0

    def set_velocity(self, value: float) -> None:
        if value > self.drv.get_velocity_parameter_limits()[1]:
            raise ValueError(f"velocity {value} above the motor's limit (MaxVelocity)")
        vmin, acc, _ = self.drv.get_velocity_parameters()
        self.drv.set_velocity_parameters(vmin, acc, value)

    def set_acceleration(self, value: float) -> None:
        if value > self.drv.get_velocity_parameter_limits()[0]:
            raise ValueError(f"acceleration {value} above the motor's limit (MaxAcceleration)")
        vmin, _, vmax = self.drv.get_velocity_parameters()
        self.drv.set_velocity_parameters(vmin, value, vmax)

    def set_home_velocity(self, value: float) -> None:
        direction, switch, _, offset = self.drv.get_move_home_parameters()
        self.drv.set_move_home_parameters(direction, switch, value, offset)


if __name__ == "__main__":
    run_instrument(ThorlabsAPT)
