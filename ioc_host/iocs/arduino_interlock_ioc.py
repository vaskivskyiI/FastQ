"""IOC: Arduino laser intensity interlock (simpleRPC firmware 4dmag
arduino/intensity_interlock, 57600 baud).

    python ioc_host/iocs/arduino_interlock_ioc.py --port COM16 --prefix FDM:INTERLOCK:
    python ioc_host/iocs/arduino_interlock_ioc.py --mock --prefix TEST:INTERLOCK:

The Arduino samples A0 (minus A1 in DifferentialMode), keeps a moving average of
AveragingSamples and trips (Triggered = 1, output pin 7 at TriggerPolarity) when
|Signal| > Threshold. ResetInterlock re-arms it; ResetExternal sends the reset
pulse (pin 8) that resets the Pharos interlock. Reading never resets anything
(4dmag's getStatus() called resetExternalDevice() - not here). The tripped state
survives an Arduino reset (EEPROM), the configuration does not.
"""
from __future__ import annotations

import random
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402

POLARITY = ("Low", "High")  # output level when tripped


class RPC:
    """simpleRPC over serial with a read timeout (the library default blocks forever)."""

    def __init__(self, port: str, baud: int, timeout: float = 3.0, reset: bool = True) -> None:
        from simple_rpc import SerialInterface

        self.h = SerialInterface(port, baudrate=baud, autoconnect=False)
        self.h._connection.timeout = timeout
        if not reset:  # DTR low while opening: the Arduino keeps running (no auto-reset)
            self.h._connection.dtr = False
        try:
            self.h.open()
        except struct.error as exc:
            self.h.close()
            raise TimeoutError(f"no simpleRPC answer on {port}") from exc

    def call(self, name, *args):
        try:
            return self.h.call_method(name, *args)
        except struct.error as exc:  # short read = timeout; the byte stream is out of step now
            raise TimeoutError(f"{name}: no reply") from exc

    def close(self):
        self.h.close()


class SimRPC:
    """The firmware's state machine without the ADC (Signal is noise around 10 counts); firmware
    defaults, but it starts ARMED (the firmware starts tripped): a tripped simulated interlock would
    make the lab's interlock suspender hold every simulated run."""

    def __init__(self) -> None:
        self.s = {"Threshold": 30, "AveragingSamples": 10, "AveragingDelay": 1, "MonitoringInterval": 1,
                  "TriggeringEnabled": True, "TriggerPolarity": False, "AutomaticResetEnabled": False,
                  "AutomaticResetDelay": 5000, "ExternalResetPulseDuration": 100, "DifferentialMode": False,
                  "AnalogReference": 1}
        self.triggered, self.external_resets = False, 0

    def call(self, name, *args):
        if name == "getCurrentValue":
            value = int(10 + random.gauss(0, 2))
            if self.s["TriggeringEnabled"] and abs(value) > self.s["Threshold"]:
                self.triggered = True
            return value
        if name == "getInterlockStatus":
            return self.triggered
        if name == "isSystemReady":
            return True
        if name == "resetInterlock":
            was, self.triggered = self.triggered, False
            return was
        if name == "forceTrigger":
            self.triggered = True
            return True
        if name == "resetExternalDevice":
            time.sleep(self.s["ExternalResetPulseDuration"] / 1000)
            self.external_resets += 1
            return True
        key = name[3:]
        if name.startswith("set"):
            self.s[key] = type(self.s[key])(args[0])
        return self.s[key]


def _rw(name, cast=lambda v: v):
    """getter/setter pair of a firmware value (the setters return what was applied)."""
    return dict(get=lambda s: s.drv.call("get" + name), set=lambda s, v: s.drv.call("set" + name, cast(v)))


class ArduinoInterlock(Instrument):
    TYPE = "arduino_interlock"
    DESCRIPTION = "Arduino laser intensity interlock: blocks the laser when the photodiode signal exceeds a threshold"
    DEFAULT_PREFIX = "FDM:INTERLOCK:"
    POLL = 0.5

    Triggered = Param(bool, event="On", get=lambda s: s.drv.call("getInterlockStatus"), kind="hinted", overview=True,
                      doc="1 = interlock tripped (laser blocked)")
    Signal = Param(int, units="counts", get=lambda s: s.drv.call("getCurrentValue"), overview=True, trend=True,
                   doc="moving-average ADC signal (A0 - A1 in DifferentialMode, else A0)")
    Ready = Param(bool, get=lambda s: s.drv.call("isSystemReady"), poll=5.0, kind="config",
                  doc="averaging buffer filled (tripping is only checked after that)")
    # Everything that decides whether / when the interlock trips or re-arms is protected (it guards
    # the sample and people against the laser); only the reset pulse length is merely "confirm".
    Threshold = Param(int, units="counts", limits=(0, 1023), poll=0, kind="config", overview=True, safety="protected",
                      doc="trip when |Signal| > Threshold", **_rw("Threshold", int))
    AveragingSamples = Param(int, limits=(1, 100), poll=0, kind="config", safety="protected", refresh=["Ready"],
                             doc="moving-average length (a change refills the buffer: Ready = 0 meanwhile)",
                             **_rw("AveragingSamples", int))
    AveragingDelayMs = Param(int, units="ms", limits=(0, 100), poll=0, kind="config", safety="protected",
                             doc="delay before each sample", **_rw("AveragingDelay", int))
    MonitoringIntervalMs = Param(int, units="ms", limits=(0, 60000), poll=0, kind="config", safety="protected",
                                 doc="sampling interval", **_rw("MonitoringInterval", int))
    TriggeringEnabled = Param(bool, poll=0, kind="config", overview=True, safety="protected",
                              doc="0 = monitor only, never trip (for adjusting the threshold)",
                              **_rw("TriggeringEnabled", bool))
    TriggerPolarity = Param("enum", choices=POLARITY, poll=0, kind="config", safety="protected",
                            get=lambda s: POLARITY[int(bool(s.drv.call("getTriggerPolarity")))],
                            set=lambda s, v: s.drv.call("setTriggerPolarity", v == "High"),
                            doc="interlock output level when tripped (firmware default Low)")
    DifferentialMode = Param(bool, poll=0, kind="config", safety="protected", doc="1 = A0 - A1, 0 = A0 only (firmware default 0)",
                             **_rw("DifferentialMode", bool))
    AnalogReference = Param(int, limits=(0, 4), poll=0, kind="config", safety="protected",
                            doc="raw analogReference() argument, (Uno: 0 EXTERNAL, 1 DEFAULT, 3 INTERNAL)",
                            **_rw("AnalogReference", int))
    AutoReset = Param(bool, poll=0, kind="config", safety="protected",
                      doc="re-arm automatically after the signal stayed below threshold for AutoResetDelayMs",
                      **_rw("AutomaticResetEnabled", bool))
    AutoResetDelayMs = Param(int, units="ms", limits=(0, 3600000), poll=0, kind="config", safety="protected",
                             **_rw("AutomaticResetDelay", int))
    ExtResetPulseMs = Param(int, units="ms", limits=(1, 1000), poll=0, kind="config",
                            doc="length of the external (Pharos) reset pulse", **_rw("ExternalResetPulseDuration", int))
    ResetInterlock = Action(lambda s: s.drv.call("resetInterlock"), safety="protected", refresh=["Triggered", "Signal"],
                            doc="re-arm the interlock (lets the laser through)")
    # safe: tripping is the safe direction
    ForceTrigger = Action(lambda s: s.drv.call("forceTrigger"), safety="safe", refresh=["Triggered"],
                          doc="trip the interlock now (blocks the laser)")
    ResetExternal = Action(lambda s: s.drv.call("resetExternalDevice"), safety="protected", refresh=["Triggered"],
                           doc="send the reset pulse to the external device (Pharos interlock)")

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--port", default="COM16")
        parser.add_argument("--baud", type=int, default=57600)
        parser.add_argument("--no-reset", action="store_true",
                            help="open the port with DTR low so the Arduino is not reset (keeps its configuration)")

    def open(self) -> None:
        self.drv = SimRPC() if self.mock else RPC(self.args.port, self.args.baud, reset=not self.args.no_reset)


if __name__ == "__main__":
    run_instrument(ArduinoInterlock)
