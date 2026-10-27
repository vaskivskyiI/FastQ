"""IOC: Arduino beam shutter + camera trigger (4dmag pump shutter, simpleRPC firmware
4dmag arduino/beam_shutter/beam_shutter.ino, 57600 baud).

    python ioc_host/iocs/arduino_shutter_ioc.py --port COM21 --prefix FDM:PUMP:
    python ioc_host/iocs/arduino_shutter_ioc.py --mock --prefix TEST:PUMP:

The Trigger action sends one trigger pulse (50 us, pin 6) to the cameras (with
LaserSync on, the Arduino waits for the next laser pulse on pin 2 first, at most
TimeoutMs, and the action fails if none came) - this is the "pulser" of synchronised
camera acquisition (fastq.detectors.synced_trigger_and_read). ChopperMode: the shutter
toggles on every falling camera-shutter edge (pin 3), so consecutive frames alternate
pumped / unpumped.

Firmware facts (beam_shutter.ino): the RPC interface is trigger, toggleShutter,
setShutter, getShutter, set/getChopperMode, set/getLaserSync, setTimeout,
setTimeoutImage - no getters for the timeouts (the IOC shows what it last wrote) and
acquireImage() is not exported, so TimeoutImageMs has no effect. The timeouts are
AVR `int` (16 bit): values above 32767 ms cannot be sent (the firmware default
imageTimeout = 60000 has already overflowed). Opening the port resets the board:
shutter closed, ChopperMode / LaserSync off, Timeout 10000 ms.
"""
from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402

INT16_MAX = 32767


class RPC:
    """simpleRPC over serial with a read timeout (the library default blocks forever)."""

    def __init__(self, port: str, baud: int, timeout: float = 3.0) -> None:
        from simple_rpc import SerialInterface

        self.h = SerialInterface(port, baudrate=baud, autoconnect=False)
        self.h._connection.timeout = self.timeout = timeout
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


class SimRPC:
    def __init__(self) -> None:
        self.state = {"Shutter": 0, "ChopperMode": 0, "LaserSync": 0, "Timeout": 10000, "TimeoutImage": -5536}
        self.pulses = 0

    def call(self, name, *args, timeout=None):
        if name == "trigger":
            self.pulses += 1
            time.sleep(0.001)
            return False  # True = no laser pulse within Timeout (LaserSync)
        if name == "toggleShutter":
            self.state["Shutter"] ^= 1
            return self.state["Shutter"]
        key = name[3:]
        if name.startswith("set"):
            if not -32768 <= int(args[0]) <= INT16_MAX:
                raise struct.error("argument out of range")  # what simple_rpc raises for an AVR int
            self.state[key] = int(args[0])
        return self.state[key]


class ArduinoShutter(Instrument):
    TYPE = "arduino_shutter"
    DESCRIPTION = "Arduino beam shutter (relays) and camera trigger output, optionally synchronised to the laser"
    DEFAULT_PREFIX = "FDM:PUMP:"
    POLL = 2.0
    ophyd = {"pulser": "Trigger"}

    # opens the pump beam onto the sample -> protected; closing has its own one-click action
    Shutter = Param(bool, get=lambda s: s.drv.call("getShutter"), set=lambda s, v: s.drv.call("setShutter", int(v)),
                    kind="normal", overview=True, safety="protected", doc="1 = open (beam passes)")
    ChopperMode = Param(bool, get=lambda s: s.drv.call("getChopperMode"), safety="protected",
                        set=lambda s, v: s.drv.call("setChopperMode", int(v)), kind="config", overview=True,
                        doc="toggle the shutter with every camera exposure (pumped/unpumped pairs; opens it "
                            "every other frame)")
    LaserSync = Param(bool, get=lambda s: s.drv.call("getLaserSync"),
                      set=lambda s, v: s.drv.call("setLaserSync", int(v)), kind="config", overview=True,
                      doc="trigger pulses wait for the next laser pulse")
    TimeoutMs = Param(int, units="ms", limits=(0, INT16_MAX), writable=True, initial=10000, safety="safe",
                      set="set_timeout", kind="config",
                      doc="how long a trigger waits for the laser (firmware default 10000; shows the last value "
                          "written - the firmware cannot report it)")
    TimeoutImageMs = Param(int, units="ms", limits=(0, INT16_MAX), writable=True, safety="safe",
                           set=lambda s, v: s.drv.call("setTimeoutImage", v), kind="config",
                           doc="image timeout of the firmware's acquireImage(), which is not exported over RPC "
                               "(no effect)")
    Trigger = Action("trigger", safety="safe", refresh=[],
                     doc="send one trigger pulse to the cameras (with LaserSync: on the next laser pulse; "
                         "fails if none came within TimeoutMs)")
    Toggle = Action(lambda s: s.drv.call("toggleShutter"), safety="protected", doc="toggle the shutter")
    CloseShutter = Action(lambda s: s.drv.call("setShutter", 0), safety="safe", doc="close the shutter",
                          refresh=["Shutter"])

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--port", default="COM21")
        parser.add_argument("--baud", type=int, default=57600)

    def open(self) -> None:
        self.drv = SimRPC() if self.mock else RPC(self.args.port, self.args.baud)
        self.timeout_ms = 10000  # the board was just reset (DTR): firmware default

    def set_timeout(self, ms: int) -> None:
        self.drv.call("setTimeout", ms)
        self.timeout_ms = ms

    def trigger(self) -> None:
        # the firmware blocks up to Timeout ms waiting for the laser: the serial read must wait longer
        timed_out = self.drv.call("trigger", timeout=self.timeout_ms / 1000 + 2.0)
        if timed_out:
            raise RuntimeError(f"no laser pulse within {self.timeout_ms} ms (LaserSync) - no trigger sent")


if __name__ == "__main__":
    run_instrument(ArduinoShutter)
