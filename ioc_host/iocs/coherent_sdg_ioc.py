"""IOC: Coherent SDG Elite synchronisation & delay generator (amplifier timing; RS-232 via VISA).

    python ioc_host/iocs/coherent_sdg_ioc.py --resource ASRL1::INSTR --prefix FDM:SDG:
    python ioc_host/iocs/coherent_sdg_ioc.py --mock --prefix TEST:SDG:

Protocol as in 4dmag fdm/coherent.py: CR-terminated ASCII, every command is
answered ("Ok" for accepted settings). `status?` returns 11 comma-separated
fields: output 1..3 enabled, output 1..3 delay, trigger division factor,
BWD switch, BWD flags (3 characters: photodiode 1, photodiode 2, interlock
latch), single-shot mode, RF sync. The whole status is read once per poll.

Delay and BWD-threshold setting use the commands of the SLAC EPICS driver for the
(newer, 8-channel) SDG Elite, github.com/mpdunning/sdgEliteIOC sdgEliteV1/V2.proto:
`set:del:c<n> <ns, 2 decimals>` (0..817611 ns) and `set:bwd:lv <0..100 %>`. Not run
on this 3-channel unit yet: a command it does not know is not answered "Ok" and the
put fails (LastError) without effect. Not documented anywhere we found, so not
implemented: pulse widths, setting the trigger division factor, RF sync settings.

SAFETY: the SDG times the amplifier Pockels cells - a wrong delay, an enabled
output, a single shot or a reset BWD interlock can make the amplifier emit or
damage it, so every write is "protected".
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402

N_OUT = 3
DELAY_MAX_NS = 817611.0  # sdgEliteIOC DRVH


class Driver:
    def __init__(self, resource: str, baud: int) -> None:
        import pyvisa

        self.inst = pyvisa.ResourceManager().open_resource(resource)
        if resource.upper().startswith("ASRL"):
            self.inst.baud_rate = baud
        self.inst.read_termination = self.inst.write_termination = "\r"
        self.inst.timeout = 2000

    def query(self, cmd: str) -> str:
        return self.inst.query(cmd).strip("\0\r\n ")  # newer firmware frames replies with a leading NUL

    def close(self):
        self.inst.close()


class SimDriver:
    def __init__(self) -> None:
        self.out, self.delay, self.single, self.shots = [1, 1, 0], [100.0, 250.5, 0.0], 0, 0
        self.latch, self.bwd_level = 0, 50

    def query(self, cmd: str) -> str:
        cmd = cmd.strip().lower()
        if cmd == "status?":
            return ",".join(map(str, [*self.out, *(f"{d:.2f}" for d in self.delay), 1, 1, f"11{self.latch}",
                                      self.single, 1]))
        try:
            if cmd.startswith("set:del:c"):
                ch, value = cmd[9:].split()
                if not 0 <= float(value) <= DELAY_MAX_NS:
                    return "Error"
                self.delay[int(ch) - 1] = round(float(value), 2)
            elif cmd.startswith("set:bwd:lv "):
                self.bwd_level = int(cmd.split()[1])
            elif cmd.startswith("set:c"):
                ch, state = cmd[5:].split()
                self.out[int(ch) - 1] = int(state)
            elif cmd.startswith("set:mode "):
                self.single = int(cmd.split()[1])
            elif cmd == "man:trig":
                self.shots += 1
            elif cmd == "reset:bwd":
                self.latch = 0
            else:
                return "Error"
        except (ValueError, IndexError):
            return "Error"
        return "Ok"


def _field(i: int, conv=float):
    return lambda s: conv(s.status()[i])


def _output(n: int) -> Param:
    return Param(bool, get=_field(n - 1, int), set=lambda s, v: s.command(f"set:c{n} {int(v)}"), overview=True,
                 safety="protected", doc=f"output channel {n} enabled (Pockels-cell trigger)")


def _delay(n: int) -> Param:
    return Param(float, units="ns", precision=2, limits=(0.0, DELAY_MAX_NS), get=_field(N_OUT + n - 1),
                 set=lambda s, v: s.command(f"set:del:c{n} {v:.2f}"), kind="config", safety="protected",
                 positioner={"tolerance": 0.006, "settle_s": 0.0, "timeout_s": 10.0},
                 doc=f"output {n} delay (set:del:c{n}; units as in the SLAC SDG Elite driver - check on first use)")


class CoherentSDG(Instrument):
    TYPE = "coherent_sdg"
    DESCRIPTION = "Coherent SDG Elite synchronisation & delay generator (amplifier timing)"
    DEFAULT_PREFIX = "FDM:SDG:"
    POLL = 2.0

    Output1, Output2, Output3 = _output(1), _output(2), _output(3)
    Delay1, Delay2, Delay3 = _delay(1), _delay(2), _delay(3)
    TriggerDivision = Param(int, get=_field(6, lambda v: int(float(v))), kind="config",
                            doc="trigger division factor (read only: no documented command sets it)")
    BwdSwitch = Param(int, get=_field(7, lambda v: int(float(v))), kind="config", doc="BWD (back-reflection) switch")
    BwdPd1 = Param(bool, get=lambda s: int(s.status()[8][0]), doc="BWD photodiode 1 flag")
    BwdPd2 = Param(bool, get=lambda s: int(s.status()[8][1]), doc="BWD photodiode 2 flag")
    BwdLatch = Param(bool, get=lambda s: int(s.status()[8][2]), overview=True,
                     doc="BWD interlock latched (ResetBwd clears it)")
    BwdThreshold = Param(int, units="%", limits=(0, 100), writable=True, kind="config", safety="protected",
                         set=lambda s, v: s.command(f"set:bwd:lv {int(v)}"),
                         doc="BWD photodiode threshold (set:bwd:lv); the unit does not report it - shows what was "
                             "last written by this IOC")
    SingleShot = Param(bool, get=_field(9, int), set=lambda s, v: s.command(f"set:mode {int(v)}"), overview=True,
                       safety="protected", doc="1 = single-shot mode (FireShot fires one pulse), 0 = continuous")
    RfSync = Param(int, get=_field(10, lambda v: int(float(v))), kind="config", doc="RF sync status")
    StatusRaw = Param(str, get=lambda s: ",".join(s.status()), kind="omitted", doc="last status? reply")

    FireShot = Action(lambda s: s.command("man:trig"), safety="protected", doc="fire one shot (single-shot mode)",
                      refresh=[])
    ResetBwd = Action(lambda s: s.command("reset:bwd"), safety="protected",
                      doc="reset the BWD interlock latch (re-arms the amplifier - find the back reflection first)")

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--resource", default="ASRL1::INSTR", help="VISA resource")
        parser.add_argument("--baud", type=int, default=9600, help="serial baud rate (4dmag: VISA default 9600)")

    def open(self) -> None:
        self.drv = SimDriver() if self.mock else Driver(self.args.resource, self.args.baud)
        self._status, self._status_t = None, 0.0
        self.status()  # 4dmag connect() reads the status too

    def status(self) -> list[str]:
        """One status? per poll round (the params of a poll group are read within ms)."""
        if self._status is None or time.monotonic() - self._status_t > 0.3:
            fields = self.drv.query("status?").split(",")
            if len(fields) < 11 or len(fields[8].strip()) < 3:
                raise ValueError(f"unexpected status? reply {','.join(fields)!r}")
            self._status, self._status_t = [f.strip("\0\r\n ") for f in fields], time.monotonic()
        return self._status

    def command(self, cmd: str) -> None:
        reply = self.drv.query(cmd)
        self._status = None  # the next read fetches the new state
        if reply != "Ok":
            raise RuntimeError(f"{cmd!r} answered {reply!r}")


if __name__ == "__main__":
    run_instrument(CoherentSDG)
