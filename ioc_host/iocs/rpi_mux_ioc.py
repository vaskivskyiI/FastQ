"""IOC: Raspberry Pi GPIO switch / multiplexer board (sample wiring: nTron / pTron / CCM boards),
remote GPIO over the network through the pigpio daemon on the Pi (`sudo pigpiod` there).

    python ioc_host/iocs/rpi_mux_ioc.py --host 192.168.1.50 --pin vcc=4 --pin ccm=17 --pin ntron=27 \
        --pin protection=22 --ccm-pins 5,6 --prefix FASTQ:RPI1:
    python ioc_host/iocs/rpi_mux_ioc.py --mock --pin vcc=4 --pin ccm=17 --pin ntron=27 --pin protection=22 \
        --pin se_plus=23 --pin se_minus=24 --ntron-pins 20,21 --ccm-pins 5,6,13 --prefix TEST:RPI1:

As TransportMeasure raspberry.py (tab "Raspberry Pi"): every GPIO 2-27 is a plain
output; named switches (Vcc, CCM, nTron, 1 MOhm protection, SE+, SE-) are single
pins; a MUX channel k (1..4 with 2 address pins, 1..8 with 3) puts the bits of k-1
on the address pins, first pin = LSB ("pTron on PCB with 2 MUX" / "... 3 MUX").
Pin numbers are chosen per board in the GUI (spin boxes, no saved defaults), so here
they come from the command line; unconfigured switches read nothing and refuse puts.

pigpio directly (gpiozero's PiGPIOFactory is a layer over it): reading a level does
not reconfigure the pin, so connecting writes nothing - gpiozero's LED() would set
every pin to an output at connect. Pins keep their state when the IOC stops.
As in TransportMeasure there is no "MUX off" state: all address pins low = channel 1.
Wiring shows the configured pins.
Safety: Vcc and the 1 MOhm protection switch and raw GPIO writes "protected" (they power or
unprotect the sample, or bypass the named wiring); MUX channels and the other lines "confirm".
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402

GPIOS = range(2, 28)
SWITCHES = {"Vcc": "vcc", "Ccm": "ccm", "Ntron": "ntron", "Protection": "protection", "SePlus": "se_plus",
            "SeMinus": "se_minus"}
SWITCH_SAFETY = {"Vcc": "protected", "Protection": "protected"}
SWITCH_DOCS = {"Vcc": "supply (Vcc)", "Ccm": "CCM line", "Ntron": "nTron line", "Protection": "1 MOhm protection",
               "SePlus": "SE+ line", "SeMinus": "SE- line"}


class PiGPIO:
    def __init__(self, host: str, port: int) -> None:
        import pigpio

        self.pi = pigpio.pi(host, port, show_errors=False)
        if not self.pi.connected:
            raise ConnectionError(f"no pigpio daemon at {host}:{port} (run 'sudo pigpiod' on the Pi)")

    def read_bank(self) -> int:
        return self.pi.read_bank_1()

    def write(self, gpio: int, level: int) -> None:
        self.pi.write(gpio, level)  # also makes the pin an output

    def close(self):
        self.pi.stop()


class SimGPIO:
    def __init__(self) -> None:
        self.bank = 0

    def read_bank(self) -> int:
        return self.bank

    def write(self, gpio: int, level: int) -> None:
        self.bank = self.bank | (1 << gpio) if level else self.bank & ~(1 << gpio)


class RpiMux(Instrument):
    TYPE = "rpi_mux"
    DESCRIPTION = "Raspberry Pi GPIO switch / MUX board (sample lines, CCM / nTron multiplexers) via pigpio"
    DEFAULT_PREFIX = "FASTQ:RPI1:"
    POLL = 1.0
    ophyd = {"positioner": "CcmChannel"}

    Wiring = Param(str, poll=0, get=lambda s: s.wiring(), kind="config", doc="configured pins (BCM numbering)")
    CcmChannel = Param(int, limits=(1, 8), get=lambda s: s.mux_get("ccm"), set=lambda s, v: s.mux_set("ccm", v),
                       positioner={"readback": "CcmChannel", "tolerance": 0.5, "settle_s": 0.0, "timeout_s": 10.0},
                       overview=True, kind="hinted", safety="confirm",
                       doc="CCM multiplexer channel (1..4 with 2 address pins, 1..8 with 3)")
    NtronChannel = Param(int, limits=(1, 4), get=lambda s: s.mux_get("ntron"), set=lambda s, v: s.mux_set("ntron", v),
                         positioner={"readback": "NtronChannel", "tolerance": 0.5, "settle_s": 0.0, "timeout_s": 10.0},
                         overview=True, safety="confirm", doc="nTron multiplexer channel (1..4)")
    for _n, _key in SWITCHES.items():
        locals()[_n] = Param(bool, get=lambda s, k=_key: s.pin_get(k), set=lambda s, v, k=_key: s.pin_set(k, v),
                             overview=True, kind="config", safety=SWITCH_SAFETY.get(_n, "confirm"),
                             doc=SWITCH_DOCS[_n])
    for _g in GPIOS:
        locals()[f"Gpio{_g}"] = Param(bool, get=lambda s, g=_g: s.level(g), set=lambda s, v, g=_g: s._write(g, int(v)),
                                      poll=2.0, kind="omitted", safety="protected",
                                      doc=f"raw GPIO {_g} (BCM numbering)")
    del _n, _key, _g
    AllOff = Action("all_off", doc="all GPIO 2-27 low (what TransportMeasure does when the board type changes)",
                    safety="confirm")

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--host", default="raspberrypi.local", help="Raspberry Pi running pigpiod")
        parser.add_argument("--port", type=int, default=8888, help="pigpiod port")
        parser.add_argument("--pin", action="append", default=[], metavar="NAME=GPIO",
                            help=f"named switch pin, NAME in {', '.join(SWITCHES.values())} (repeatable)")
        parser.add_argument("--ccm-pins", default="", help="CCM MUX address pins, LSB first: 2 or 3 GPIOs, e.g. 5,6,13")
        parser.add_argument("--ntron-pins", default="", help="nTron MUX address pins, LSB first, e.g. 20,21")

    def __init__(self, args) -> None:
        super().__init__(args)
        self.pins = {}
        for item in args.pin:
            name, _, gpio = item.partition("=")
            if name not in SWITCHES.values() or int(gpio) not in GPIOS:
                raise SystemExit(f"--pin {item}: NAME one of {list(SWITCHES.values())}, GPIO 2..27")
            self.pins[name] = int(gpio)
        self.mux = {k: [int(g) for g in v.split(",") if g.strip()] for k, v in
                    (("ccm", args.ccm_pins), ("ntron", args.ntron_pins))}
        if any(len(p) not in (0, 2, 3) or not set(p) <= set(GPIOS) for p in self.mux.values()):
            raise SystemExit("--ccm-pins / --ntron-pins: 2 or 3 GPIOs 2..27")
        self._bank, self._bank_t = 0, 0.0

    def open(self) -> None:
        self.drv = SimGPIO() if self.mock else PiGPIO(self.args.host, self.args.port)
        self._bank_t = 0.0

    def wiring(self) -> str:
        parts = [f"{k}={g}" for k, g in self.pins.items()]
        parts += [f"{k}_mux={','.join(map(str, g))}" for k, g in self.mux.items() if g]
        return " ".join(parts) or "no pins configured"

    def level(self, gpio: int) -> int:
        if time.monotonic() - self._bank_t > 0.2:  # one read for all pins of a poll
            self._bank, self._bank_t = self.drv.read_bank(), time.monotonic()
        return (self._bank >> gpio) & 1

    def _write(self, gpio: int, level: int) -> None:
        self.drv.write(gpio, level)
        self._bank_t = 0.0

    def pin_get(self, name: str):
        return self.level(self.pins[name]) if name in self.pins else None

    def pin_set(self, name: str, value) -> None:
        if name not in self.pins:
            raise ValueError(f"no pin configured for {name} (--pin {name}=GPIO)")
        self._write(self.pins[name], int(bool(value)))

    def mux_get(self, mux: str):
        pins = self.mux[mux]
        return 1 + sum(self.level(g) << i for i, g in enumerate(pins)) if pins else None

    def mux_set(self, mux: str, channel: int) -> None:
        pins = self.mux[mux]
        if not pins:
            raise ValueError(f"no address pins configured (--{mux}-pins)")
        if not 1 <= channel <= 1 << len(pins):
            raise ValueError(f"{mux} channel {channel} outside 1..{1 << len(pins)}")
        for i, g in enumerate(pins):
            self._write(g, ((channel - 1) >> i) & 1)

    def all_off(self) -> None:
        for g in GPIOS:
            self._write(g, 0)


if __name__ == "__main__":
    run_instrument(RpiMux)
