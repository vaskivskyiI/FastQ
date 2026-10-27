"""IOC: DSC magnet power supply (RS232, Modbus-RTU style frames with CRC16;
port of 4dmag fdm/RS232/powsupply_rs232.py).

    python ioc_host/iocs/dsc_psu_ioc.py --port COM15 --prefix FDM:MAGNET:
    python ioc_host/iocs/dsc_psu_ioc.py --mock --prefix TEST:MAGNET:

Current is signed: the supply holds a magnitude plus a polarity relay, and the
sign is changed only at 0 A. A put of Current ramps from the present setpoint
in steps of at most --ramp-step A (0.5 A as in 4dmag; with the output off it is
set directly) and completes when the ramp is done; the positioner then waits
for the measured output current OutI. OutputOn/OutputOff ramp to/from the
setpoint like the original (OutputOn: 0 A, output on, ramp to the setpoint
that was put while off). Closing the IOC does NOT switch the output off.

The register map (output/polarity coils, voltage/current/OVP/OCP holding
registers, measured output input registers) is the complete set 4dmag used; no
DSC manual was found online, so nothing beyond it is implemented (the polarity
coil itself is "not in manual, contacted DSC" per 4dmag).
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402

READ_COILS, READ_HOLDING, READ_INPUT, WRITE_COIL, WRITE_REG = 0x01, 0x03, 0x04, 0x05, 0x06
REG = {"output": 0x85, "polarity": 0x8F, "voltage": 0x95, "current": 0x96, "ovp": 0x9D, "ocp": 0x9E,
       "out_voltage": 0x64, "out_current": 0x65}
POLARITY = ("Forward", "Reverse")


def crc16(data: bytes) -> bytes:
    """Modbus CRC16, low byte first."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return bytes((crc & 0xFF, crc >> 8))


class Driver:
    """Frames of the DSC protocol; values are register counts / 100 (unsigned)."""

    def __init__(self, ser, unit: int = 1) -> None:
        self.ser, self.unit = ser, unit

    def _query(self, func: int, reg: int, value: int, nreply: int) -> bytes:
        frame = bytes((self.unit, func)) + reg.to_bytes(2, "big") + int(value).to_bytes(2, "big")
        frame += crc16(frame)
        self.ser.reset_input_buffer()
        self.ser.write(frame)
        reply = self.ser.read(nreply)
        if len(reply) < nreply:
            raise TimeoutError(f"DSC: {len(reply)}/{nreply} reply bytes to {frame.hex()}")
        if reply[0] != self.unit or reply[1] != func:
            raise ValueError(f"DSC: unexpected reply {reply.hex()} to {frame.hex()}")
        if func in (WRITE_COIL, WRITE_REG) and reply != frame:  # a write is echoed
            raise ValueError(f"DSC: write not acknowledged ({reply.hex()} for {frame.hex()})")
        return reply

    def read_coil(self, name: str) -> int:
        return self._query(READ_COILS, REG[name], 1, 6)[3]

    def read_reg(self, name: str) -> float:
        func = READ_INPUT if name.startswith("out_") else READ_HOLDING
        return int.from_bytes(self._query(func, REG[name], 1, 7)[3:5], "big") / 100

    def write_coil(self, name: str, on: bool) -> None:
        self._query(WRITE_COIL, REG[name], 0xFF00 if on else 0x0000, 8)

    def write_reg(self, name: str, value: float) -> None:
        self._query(WRITE_REG, REG[name], int(round(abs(value) * 100)), 8)

    def close(self):
        self.ser.close()


class SimSerial:
    """The supply on the other end of the cable: answers frames, output follows the setpoint into 1 Ohm."""

    def __init__(self) -> None:
        self.regs = {REG["voltage"]: 2000, REG["current"]: 0, REG["ovp"]: 3000, REG["ocp"]: 1500}
        self.coils = {REG["output"]: 0, REG["polarity"]: 0}
        self.out, self.reply = 0.0, b""

    def reset_input_buffer(self):
        self.reply = b""

    def write(self, frame: bytes):
        assert crc16(frame[:6]) == frame[6:8], "bad CRC"
        unit, func, reg, val = frame[0], frame[1], int.from_bytes(frame[2:4], "big"), int.from_bytes(frame[4:6], "big")
        on = self.coils[REG["output"]]
        target = min(self.regs[REG["current"]] / 100, self.regs[REG["voltage"]] / 100) if on else 0.0
        self.out += 0.7 * (target - self.out)
        if func == WRITE_COIL:
            self.coils[reg] = int(val == 0xFF00)
            self.reply = frame
        elif func == WRITE_REG:
            self.regs[reg] = val
            self.reply = frame
        elif func == READ_COILS:
            body = bytes((unit, func, 1, self.coils[reg]))
            self.reply = body + crc16(body)
        else:
            value = {REG["out_current"]: self.out, REG["out_voltage"]: self.out * 1.0}.get(reg)
            value = self.regs[reg] if value is None else int(max(value + random.gauss(0, 0.002), 0) * 100)
            body = bytes((unit, func, 2)) + int(value).to_bytes(2, "big")
            self.reply = body + crc16(body)

    def read(self, n: int) -> bytes:
        out, self.reply = self.reply[:n], self.reply[n:]
        return out

    def close(self):
        pass


class DSCPowerSupply(Instrument):
    TYPE = "dsc_psu"
    DESCRIPTION = "DSC magnet power supply (bipolar via polarity relay; current ramped, polarity changed at 0 A)"
    DEFAULT_PREFIX = "FDM:MAGNET:"
    POLL = 1.0

    # confirm (not protected): bounded by --max-current, and every change is ramped
    Current = Param(float, units="A", precision=2, limits=(-100.0, 100.0), get="read_current", set="ramp_current",
                    positioner={"readback": "OutI", "tolerance": 0.05, "settle_s": 1.0, "timeout_s": 1200.0},
                    overview=True, refresh=["OutI", "OutV", "Polarity"],
                    doc="signed current setpoint; a put ramps to it (polarity changes at 0 A)")
    OutI = Param(float, units="A", precision=3, get=lambda s: s.drv.read_reg("out_current") * s.polarity(),
                 kind="hinted", overview=True, trend=True, label="Output current", doc="measured output current")
    OutV = Param(float, units="V", precision=3, get=lambda s: s.drv.read_reg("out_voltage") * s.polarity(),
                 overview=True, label="Output voltage", doc="measured output voltage")
    # compliance and protection limits are protected: they are what keeps the coil (and the sample) safe
    Voltage = Param(float, units="V", precision=2, limits=(0.0, 100.0), poll=0, kind="config", safety="protected",
                    get=lambda s: s.drv.read_reg("voltage"), set="ramp_voltage", refresh=["OutI", "OutV"],
                    doc="voltage limit (compliance); ramped like the current")
    OVP = Param(float, units="V", precision=2, limits=(0.0, 200.0), poll=0, kind="config", safety="protected",
                get=lambda s: s.drv.read_reg("ovp"), set=lambda s, v: s.drv.write_reg("ovp", v),
                doc="over-voltage protection")
    OCP = Param(float, units="A", precision=2, limits=(0.0, 200.0), poll=0, kind="config", safety="protected",
                get=lambda s: s.drv.read_reg("ocp"), set=lambda s, v: s.drv.write_reg("ocp", v),
                doc="over-current protection")
    Output = Param(bool, get=lambda s: s.drv.read_coil("output"), overview=True, doc="1 = output on")
    Polarity = Param("enum", choices=POLARITY, get=lambda s: 0 if s.polarity() > 0 else 1, kind="config")
    OutputOn = Action("output_on", safety="confirm", timeout_s=1200.0,
                      doc="switch the output on at 0 A, then ramp to the current setpoint")
    OutputOff = Action("output_off", safety="confirm", timeout_s=1200.0,
                       doc="ramp the current to 0 A, switch the output off")

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--port", default="COM15")
        parser.add_argument("--baud", type=int, default=9600, help="9600 in powsupply_rs232.py (DSC_PSU.py: 57600)")
        parser.add_argument("--unit", type=int, default=1, help="device id in the frames")
        parser.add_argument("--ramp-step", type=float, default=0.5, help="largest current step of a ramp (A)")
        parser.add_argument("--ramp-sleep", type=float, default=0.01, help="pause after each ramp step (s)")
        parser.add_argument("--max-current", type=float, default=100.0, help="|Current| limit (A)")
        parser.add_argument("--tolerance", type=float, default=0.05, help="positioner: |OutI - Current| (A)")

    def instance_schema(self) -> dict:
        schema = self.schema()
        for p in schema["params"]:
            if p["name"] == "Current":
                p["limits"] = [-self.args.max_current, self.args.max_current]
                p["positioner"] = {**p["positioner"], "tolerance": self.args.tolerance}
        return schema

    def open(self) -> None:
        if self.mock:
            ser = SimSerial()
        else:
            import serial

            ser = serial.Serial(self.args.port, self.args.baud, bytesize=8, stopbits=1, timeout=1.0)
        self.drv = Driver(ser, self.args.unit)

    def polarity(self) -> int:
        return 1 if self.drv.read_coil("polarity") == 0 else -1

    def read_current(self) -> float:
        return self.drv.read_reg("current") * self.polarity()

    def _step(self, name: str, value: float) -> None:
        """One ramp step as 4dmag setparam(ramp=False): write, pause, read the outputs back."""
        self.drv.write_reg(name, value)
        time.sleep(self.args.ramp_sleep)
        pol = self.polarity()
        out_v, out_i = (self.drv.read_reg(r) * pol for r in ("out_voltage", "out_current"))
        self.drv.read_reg(name)  # as 4dmag; with the reads above it also paces the ramp
        self.publish(OutI=out_i, OutV=out_v)  # progress while the put (and the lock) is held

    def ramp_current(self, value: float) -> None:
        if abs(value) > self.args.max_current:
            raise ValueError(f"|Current| {value} > --max-current {self.args.max_current}")
        step = self.args.ramp_step if self.drv.read_coil("output") else 100.0  # output off: no reason to ramp
        pol, cur = self.polarity(), self.read_current()
        for s in np.round(np.linspace(cur, value, int(abs(value - cur) / step) + 2), 2):
            if s * pol < 0:  # crossing zero: through 0 A, then flip the polarity relay
                self._step("current", 0.0)
                self.drv.write_coil("polarity", pol > 0)  # coil on = reverse
                pol = -pol
            self._step("current", abs(s))

    def ramp_voltage(self, value: float) -> None:
        step = self.args.ramp_step if self.drv.read_coil("output") else 100.0
        cur = self.drv.read_reg("voltage")
        for s in np.round(np.linspace(cur, value, int(abs(value - cur) / step) + 2), 2):
            self._step("voltage", s)

    def output_on(self) -> None:
        if self.drv.read_coil("output"):
            return
        setpoint = self.read_current()
        self.drv.write_reg("current", 0.0)
        self.drv.write_coil("output", True)
        self.ramp_current(setpoint)

    def output_off(self) -> None:
        self.ramp_current(0.0)
        self.drv.write_coil("output", False)


if __name__ == "__main__":
    run_instrument(DSCPowerSupply)
