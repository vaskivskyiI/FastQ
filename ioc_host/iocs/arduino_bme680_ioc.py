"""IOC: Arduino + Bosch BME680 environment sensor (temperature, humidity, pressure,
gas resistance; simpleRPC firmware 4dmag arduino/temperature_sensor, 57600 baud).

    python ioc_host/iocs/arduino_bme680_ioc.py --port COM10 --prefix FDM:ENV:
    python ioc_host/iocs/arduino_bme680_ioc.py --mock --prefix TEST:ENV:

The firmware measures every ReadIntervalMs (default 30 s) and returns the last
measurement; values are 0 until the first measurement after an Arduino reset
(opening the port resets it). There is no getter for the interval, so the
ReadIntervalMs PV shows what was last put (firmware default 30000).
"""
from __future__ import annotations

import random
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Instrument, Param, run_instrument  # noqa: E402


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
    def __init__(self) -> None:
        self.interval = 30000

    def call(self, name, *args):
        if name == "setReadInterval":
            self.interval = int(args[0])
            return self.interval
        return {"getTemperature": lambda: 22.0 + random.gauss(0, 0.05),
                "getHumidity": lambda: 35.0 + random.gauss(0, 0.2),
                "getPressure": lambda: 980.0 + random.gauss(0, 0.1),
                "getGas": lambda: 50000.0 + random.gauss(0, 500)}[name]()


class ArduinoBME680(Instrument):
    TYPE = "arduino_bme680"
    DESCRIPTION = "Arduino BME680 environment sensor: lab temperature, humidity, air pressure, gas resistance"
    DEFAULT_PREFIX = "FDM:ENV:"
    POLL = 5.0

    Temperature = Param(float, units="C", precision=2, get=lambda s: s.drv.call("getTemperature"), kind="hinted",
                        overview=True, trend=True, doc="air temperature")
    Humidity = Param(float, units="%", precision=1, get=lambda s: s.drv.call("getHumidity"), overview=True,
                     doc="relative humidity")
    Pressure = Param(float, units="hPa", precision=2, get=lambda s: s.drv.call("getPressure"), doc="air pressure")
    Gas = Param(float, units="Ohm", precision=0, get=lambda s: s.drv.call("getGas"), kind="config",
                doc="gas sensor resistance (air quality indicator)")
    # safe: measurement rate of a room sensor; the firmware has no other setting (only the 4 getters)
    ReadIntervalMs = Param(int, units="ms", limits=(500, 86400000), writable=True, initial=30000, safety="safe",
                           set=lambda s, v: s.drv.call("setReadInterval", v), kind="config",
                           doc="how often the Arduino measures (firmware default 30000; not readable back)")

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--port", default="COM10")
        parser.add_argument("--baud", type=int, default=57600)
        parser.add_argument("--no-reset", action="store_true",
                            help="open the port with DTR low so the Arduino is not reset")

    def open(self) -> None:
        self.drv = SimRPC() if self.mock else RPC(self.args.port, self.args.baud, reset=not self.args.no_reset)


if __name__ == "__main__":
    run_instrument(ArduinoBME680)
