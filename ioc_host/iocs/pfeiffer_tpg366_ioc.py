"""IOC: Pfeiffer Vacuum TPG 366 MaxiGauge (6 gauge channels; RS-232/USB or Ethernet).

    python ioc_host/iocs/pfeiffer_tpg366_ioc.py --port COM8 --prefix FDM:VAC:
    python ioc_host/iocs/pfeiffer_tpg366_ioc.py --host 192.168.1.50 --tcp-port 8000 --prefix FDM:VAC:
    python ioc_host/iocs/pfeiffer_tpg366_ioc.py --mock --prefix TEST:VAC:

Pfeiffer mnemonics protocol (TPG 366 communication protocol BG 5511 BEN): send
"PRX<CR><LF>", the controller answers <ACK><CR><LF> (or <NAK>), then <ENQ>
fetches the reply "s1,p1,s2,p2,...,s6,p6". Status s: 0 ok, 1 underrange, 2
overrange, 3 sensor error, 4 sensor off, 5 no sensor, 6 identification error.
Pressures and switching thresholds are published in mbar whatever unit the front
panel shows (UNI, read every 10 s). Channel settings that take six values (SEN,
FIL, GAS, DGS) are read, changed in one place and written back.
Used: PRX, UNI, TID, CID, SEN, FIL, GAS, DGS, SP1-SP6, SPS, ERR, TMP, AYT.
Not implemented: CAL/CFx (calibration factors), OFC/OFD/FSR (linear gauges),
SCx (automatic gauge control), display / interface / data-logger settings.
(The 4dmag iv_redis branch has an unfinished Modbus RTU driver that only ever
ran in simulation; this one uses the documented ASCII protocol, which the TPG 366
serves on RS-232/USB and on Ethernet port 8000.)
"""
from __future__ import annotations

import math
import random
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402

ACK, NAK, ENQ = b"\x06", b"\x15", b"\x05"
STATUS = ("OK", "Underrange", "Overrange", "Sensor error", "Sensor off", "No sensor", "Identification error")
UNITS = ("mbar", "Torr", "Pa", "Micron", "hPa", "V")
TO_MBAR = {"mbar": 1.0, "Torr": 1.33322, "Pa": 0.01, "Micron": 1.33322e-3, "hPa": 1.0, "V": float("nan")}
FILTERS = ("Off", "Fast", "Normal", "Slow")
GASES = ("N2/air", "Argon", "Hydrogen", "Helium", "Neon", "Krypton", "Xenon", "Other (CAL)")
SP_ASSIGN = ("Off", "On", "Channel 1", "Channel 2", "Channel 3", "Channel 4", "Channel 5", "Channel 6")
ERRORS = ("ERROR (see front panel)", "NO HWR (hardware not installed)", "PAR (parameter not permitted)",
          "SYN (wrong syntax)")


class Driver:
    """ASCII mnemonics with the ACK/ENQ handshake over a serial port or TCP."""

    def __init__(self, port: str, baud: int, host: str, tcp_port: int) -> None:
        self.sock = self.ser = None
        if host:
            self.sock = socket.create_connection((host, tcp_port), timeout=2.0)
        else:
            import serial

            self.ser = serial.Serial(port, baud, timeout=2.0)
        self.buf = b""

    def _write(self, data: bytes):
        (self.sock.sendall if self.sock else self.ser.write)(data)

    def _readline(self) -> bytes:
        if self.ser:
            line = self.ser.read_until(b"\r\n")
            if not line.endswith(b"\r\n"):
                raise TimeoutError(f"no reply (got {line!r})")
            return line[:-2]
        while b"\r\n" not in self.buf:
            chunk = self.sock.recv(256)
            if not chunk:
                raise ConnectionError("TPG closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def query(self, mnemonic: str) -> str:
        """mnemonic [,parameters] -> the reply after <ENQ> (for a setting: its new value)."""
        if self.ser:
            self.ser.reset_input_buffer()  # also drops the continuous output after power-on
        self.buf = b""
        self._write(mnemonic.encode() + b"\r\n")
        ack = self._readline()
        if ack != ACK:
            raise RuntimeError(f"{mnemonic}: {'NAK (error, see Error)' if ack == NAK else repr(ack)}")
        self._write(ENQ)
        return self._readline().decode(errors="replace").strip()

    def close(self):
        (self.sock or self.ser).close()


class SimDriver:
    """The controller's replies: four gauges, switching functions with hysteresis (on below the lower
    threshold, off above the upper one), degas for 3 min, gauges that can be switched off."""

    def __init__(self) -> None:
        self.base = [1e-3, 5e-7, 2e-9, 980.0, None, None]  # mbar
        self.ids = ["TPR", "IKR", "IMR", "CMR", "noSENSOR", "noSENSOR"]
        self.sen = [0, 2, 2, 0, 0, 0]  # 0 = cannot be switched (Pirani, linear)
        self.fil, self.gas, self.unit = [2] * 6, [0] * 6, 0
        self.degas_until = [0.0] * 6
        self.sp = [[2 + i, 1e-6 * 10 ** i, 2e-6 * 10 ** i] for i in range(4)] + [[0, 1e-3, 2e-3]] * 2
        self.sps = [0] * 6
        self.err = "0000"

    def _p(self, i):
        p = self.base[i]
        return None if p is None else p * math.exp(0.02 * random.gauss(0, 1))

    def _status(self, i):
        if self.base[i] is None:
            return 5
        return 4 if self.sen[i] == 1 else 0

    def _six(self, args, valid):
        vals = [int(x) for x in args]
        if len(vals) != 6 or any(v not in valid for v in vals):
            self.err = "0010"
            raise RuntimeError("NAK (error, see Error)")
        return vals

    def query(self, mnemonic: str) -> str:
        head, *args = [x.strip() for x in mnemonic.split(",")]
        u = TO_MBAR[UNITS[self.unit]]
        if head == "PRX":
            out = []
            for i in range(6):
                p = self._p(i)
                out += [str(self._status(i)), f"{(p if p is not None and self._status(i) == 0 else 2e-2) / u:.4E}"]
            for k, (assign, low, high) in enumerate(self.sp):  # switching functions follow the pressures
                if assign >= 2 and self.base[assign - 2] is not None:
                    p = self.base[assign - 2]
                    self.sps[k] = 1 if p < low else 0 if p > high else self.sps[k]
                else:
                    self.sps[k] = int(assign == 1)
            return ",".join(out)
        if head == "UNI":
            if args:
                self.unit = int(args[0])
            return str(self.unit)
        if head == "TID":
            return ",".join(self.ids)
        if head == "CID":
            return ",".join(f"GAUGE{i + 1}" for i in range(6))
        if head == "SEN":
            if args:
                for i, v in enumerate(self._six(args, (0, 1, 2))):
                    if v and self.sen[i]:
                        self.sen[i] = v
            return ",".join(map(str, self.sen))
        if head == "FIL":
            if args:
                self.fil = self._six(args, (0, 1, 2, 3))
            return ",".join(map(str, self.fil))
        if head == "GAS":
            if args:
                self.gas = self._six(args, range(8))
            return ",".join(map(str, self.gas))
        if head == "DGS":
            now = time.monotonic()
            if args:
                for i, v in enumerate(self._six(args, (0, 1))):
                    self.degas_until[i] = now + 180.0 if v else 0.0
            return ",".join(str(int(t > now)) for t in self.degas_until)
        if head.startswith("SP") and head[2:].isdigit():
            k = int(head[2:]) - 1
            if args:
                self.sp[k] = [int(args[0]), float(args[1]) * u, float(args[2]) * u]
            a, low, high = self.sp[k]
            return f"{a},{low / u:.4E},{high / u:.4E}"
        if head == "SPS":
            return ",".join(map(str, self.sps))
        if head == "ERR":
            err, self.err = self.err, "0000"
            return err
        if head == "TMP":
            return "31"
        if head == "AYT":
            return "TPG366,PTG28770,44990000,010100,010100"
        self.err = "0001"
        raise RuntimeError(f"{mnemonic}: NAK (error, see Error)")


def _channel_params(n: int) -> dict:
    return {
        f"Pressure{n}": Param(float, units="mbar", precision=3, get=lambda s: s.pressure(n), kind="hinted",
                              overview=True, trend=True, doc=f"gauge {n} (NaN unless status OK)"),
        f"Status{n}": Param("enum", choices=STATUS, get=lambda s: s.status(n), overview=True, doc=f"gauge {n} status"),
        # confirm: switching a gauge off loses its reading (and may trip a suspender / interlock on it)
        f"GaugeOn{n}": Param(bool, poll=5.0, get=lambda s: s.six("SEN")[n - 1] == 2, kind="config",
                             set=lambda s, v: s.drv.query("SEN," + ",".join("2" if v and i == n else "1" if i == n
                                                                             else "0" for i in range(1, 7))),
                             refresh=[f"Status{n}"],
                             doc=f"gauge {n} high voltage / emission on (SEN; Pirani and linear gauges cannot)"),
        f"Filter{n}": Param("enum", choices=FILTERS, poll=0, get=lambda s: s.six("FIL")[n - 1], kind="config",
                            set=lambda s, v: s.write_six("FIL", n, FILTERS.index(v)), safety="safe",
                            doc=f"gauge {n} measured-value filter"),
        # confirm: changes the calibration of the reading (a scan sees a jump)
        f"GasType{n}": Param("enum", choices=GASES, poll=0, get=lambda s: s.six("GAS")[n - 1], kind="config",
                             set=lambda s, v: s.write_six("GAS", n, GASES.index(v)),
                             doc=f"gauge {n} gas correction (not for linear gauges)"),
        f"Degas{n}": Action(lambda s: s.write_six("DGS", n, 1), refresh=["DegasActive"],
                            doc=f"degas gauge {n} for 3 minutes (DGS; hot-cathode gauges only - the reading is "
                                "off meanwhile)"),
    }


def _switch_params(k: int) -> dict:
    # switching functions are protected: their relays are wired to valves, pumps and interlocks
    return {
        f"Relay{k}": Param(bool, get=lambda s: s.six("SPS", 0.3)[k - 1], overview=True,
                           doc=f"switching function {k} state (SPS)"),
        f"SPChannel{k}": Param("enum", choices=SP_ASSIGN, poll=0, get=lambda s: s.switch(k)[0], kind="config",
                               set=lambda s, v: s.write_switch(k, assign=SP_ASSIGN.index(v)), safety="protected",
                               refresh=[f"Relay{k}"], doc=f"switching function {k}: gauge it follows, or always off/on"),
        f"SPLow{k}": Param(float, units="mbar", precision=3, poll=0, get=lambda s: s.switch(k)[1], kind="config",
                           set=lambda s, v: s.write_switch(k, low=v), safety="protected", refresh=[f"Relay{k}"],
                           doc=f"switching function {k} lower threshold (on below it)"),
        f"SPHigh{k}": Param(float, units="mbar", precision=3, poll=0, get=lambda s: s.switch(k)[2], kind="config",
                            set=lambda s, v: s.write_switch(k, high=v), safety="protected", refresh=[f"Relay{k}"],
                            doc=f"switching function {k} upper threshold (off above it)"),
    }


class TPG366(Instrument):
    TYPE = "pfeiffer_tpg366"
    DESCRIPTION = "Pfeiffer TPG 366 MaxiGauge vacuum gauge controller (6 channels, 6 switching functions)"
    DEFAULT_PREFIX = "FDM:VAC:"
    POLL = 1.0

    for _n in range(1, 7):
        locals().update(_channel_params(_n))
    for _n in range(1, 7):
        locals().update(_switch_params(_n))
    del _n
    # safe: display unit only - every PV here stays in mbar
    Unit = Param("enum", choices=UNITS, get="unit_code", set="write_unit", kind="config", safety="safe",
                 doc="unit on the controller's display (values here are converted to mbar)")
    Gauges = Param(str, get=lambda s: s.drv.query("TID"), poll=0, kind="config",
                   doc="gauge types on channels 1-6 (TID)")
    Names = Param(str, get=lambda s: s.drv.query("CID"), poll=0, kind="config", doc="channel names 1-6 (CID)")
    DegasActive = Param(str, poll=5.0, get=lambda s: ",".join(str(i + 1) for i, v in enumerate(s.six("DGS", 0)) if v)
                        or "none", doc="channels degassing now")
    Error = Param(str, poll=5.0, get="error_status", doc="controller error status (ERR; cleared when read)")
    DeviceTemp = Param(float, units="C", precision=0, poll=10.0, get=lambda s: float(s.drv.query("TMP")),
                       kind="config", doc="inner temperature of the controller")
    Identity = Param(str, poll=0, get=lambda s: s.drv.query("AYT"), kind="config",
                     doc="model, part number, serial, firmware, hardware (AYT)")

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--port", default="COM8", help="serial port (when no --host)")
        parser.add_argument("--baud", type=int, default=9600)
        parser.add_argument("--host", default="", help="Ethernet: controller address")
        parser.add_argument("--tcp-port", type=int, default=8000)

    def open(self) -> None:
        a = self.args
        self.drv = SimDriver() if self.mock else Driver(a.port, a.baud, a.host, a.tcp_port)
        self._prx, self._prx_t, self.unit, self._unit_t = None, 0.0, None, 0.0
        self._cache: dict[str, tuple[float, list[int]]] = {}

    def _unit(self) -> str:
        if self.unit is None or time.monotonic() - self._unit_t > 10.0:  # the front-panel unit rarely changes
            self.unit, self._unit_t = UNITS[int(self.drv.query("UNI"))], time.monotonic()
        return self.unit

    def _read(self) -> list[str]:
        """One PRX per poll round for all 12 values."""
        if self._prx is None or time.monotonic() - self._prx_t > 0.3:
            self._unit()
            fields = [f.strip() for f in self.drv.query("PRX").split(",")]
            if len(fields) != 12:
                raise ValueError(f"unexpected PRX reply {','.join(fields)!r}")
            self._prx, self._prx_t = fields, time.monotonic()
        return self._prx

    def unit_code(self) -> int:
        return UNITS.index(self._unit())

    def write_unit(self, unit: str) -> None:
        self.drv.query(f"UNI,{UNITS.index(unit)}")
        self.unit, self._prx = None, None

    def status(self, ch: int) -> int:
        return int(self._read()[2 * ch - 2])

    def pressure(self, ch: int) -> float:
        f = self._read()
        return float(f[2 * ch - 1]) * TO_MBAR[self.unit] if f[2 * ch - 2] == "0" else float("nan")

    def six(self, mnemonic: str, max_age: float = 1.0) -> list[int]:
        """a six-value setting / status, read once per poll round."""
        t, v = self._cache.get(mnemonic, (0.0, None))
        if v is None or time.monotonic() - t > max_age:
            v = [int(x) for x in self.drv.query(mnemonic).split(",")[:6]]
            self._cache[mnemonic] = (time.monotonic(), v)
        return v

    def write_six(self, mnemonic: str, ch: int, value: int) -> None:
        self._cache.pop(mnemonic, None)
        vals = self.six(mnemonic)
        vals[ch - 1] = int(value)
        self._cache.pop(mnemonic, None)
        self.drv.query(mnemonic + "," + ",".join(map(str, vals)))

    def switch(self, k: int) -> tuple[int, float, float]:
        a, low, high = (x.strip() for x in self.drv.query(f"SP{k}").split(","))
        u = TO_MBAR[self._unit()]
        return int(a), float(low) * u, float(high) * u

    def write_switch(self, k: int, assign=None, low=None, high=None) -> None:
        a, lo, hi = self.switch(k)
        a = a if assign is None else assign
        lo = lo if low is None else low
        hi = hi if high is None else high
        if lo > hi:
            raise ValueError(f"lower threshold {lo} > upper threshold {hi}")
        u = TO_MBAR[self._unit()]
        if math.isnan(u):
            raise ValueError("thresholds cannot be converted while the controller shows volts (Unit = V)")
        self.drv.query(f"SP{k},{a},{lo / u:.4E},{hi / u:.4E}")
        self._cache.pop("SPS", None)

    def error_status(self) -> str:
        e = self.drv.query("ERR").strip()
        return ", ".join(name for bit, name in zip(e, ERRORS) if bit == "1") or "no error"


if __name__ == "__main__":
    run_instrument(TPG366)
