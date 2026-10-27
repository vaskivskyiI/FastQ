"""In-memory fake Keithley 6221 (+2182A) transport for testing
iocs/keithley_ioc.py without hardware:  python iocs/keithley_ioc.py --mock

Implements write()/query() like a pyvisa resource. Simulates arm ->
INIT:IMM -> buffer filling over time -> TRAC:DATA?, SOUR:SWE:ABOR, OUTP,
the waveform generator (SOUR:WAVE:ARM/INIT/ABOR), SYST:ERR? (unknown commands
produce -113 "Undefined header"), the 2182A serial pass-through, and queries
of every setting written before ("SOUR:CURR:RANG?" answers the last
"SOUR:CURR:RANG ..."; defaults like a 6221 after *RST). Every write is appended to .writes and echoed
to stdout as "MOCK WRITE ..." so tests can assert what was sent.
"""
from __future__ import annotations

import random
import threading
import time

KNOWN_PREFIXES = (
    "*CLS", "*RST", "TRAC:CLE", "TRAC:POIN", "FORM:ELEM", "UNIT", "SOUR:CURR", "SOUR:SWE:",
    "SOUR:DELT:", "SOUR:PDEL:", "SOUR:DCON:", "INIT:IMM", "OUTP", "SYST:COMM:SER:SEND",
    "SOUR:LIST:CURR", "SOUR:DEL ", "SOUR:WAVE:",
)
DEFAULTS = {"SOUR:CURR": "0", "SOUR:CURR:COMP": "10", "SOUR:CURR:RANG": "2E-3", "SOUR:CURR:RANG:AUTO": "0",
            "OUTP:LTE": "1", "OUTP:ISH": "OLOW"}


class MockK6221:
    def __init__(self, speedup: float = 1.0, verbose: bool = True) -> None:
        self.speedup = speedup
        self.verbose = verbose
        self.writes: list[str] = []
        self.errors: list[str] = []
        self.settings: dict[str, str] = dict(DEFAULTS)
        self.armed_mode: str | None = None
        self.running_since: float | None = None
        self.output_on = False
        self.buffer: list[tuple[float, float]] = []
        self.current_list: list[float] = []  # SOUR:LIST:CURR (+ :APP)
        self._serial_reply = ""
        self._lock = threading.Lock()

    # -------- helpers --------
    def _n(self) -> int:
        n = int(float(self.settings.get("TRAC:POIN", "0")))
        if self.settings.get("SOUR:DELT:COUN", "").upper() == "INF":
            return 10**9  # continuous: never completes by itself
        return n

    def _period(self) -> float:
        mode = self.armed_mode
        if mode == "DELT":
            return 2 * float(self.settings.get("SOUR:DELT:DEL", "0.02")) + 0.02
        if mode == "PDEL":
            return int(self.settings.get("SOUR:PDEL:INT", "5")) / 50.0
        if mode == "DCON":
            return 2 * float(self.settings.get("SOUR:DCON:DEL", "0.02")) + 0.02
        return 0.1

    def _points(self) -> int:
        if self.running_since is None:
            return len(self.buffer)
        elapsed = (time.monotonic() - self.running_since) * self.speedup
        n = min(self._n(), int(elapsed / self._period()))
        while len(self.buffer) < n:
            i = len(self.buffer)
            self.buffer.append((self._reading(i), round(i * self._period(), 4)))
        if n >= self._n():
            self.running_since = None
            self.armed_mode = None
            self.output_on = False
        return n

    def _reading(self, i: int) -> float:
        """Pulse delta sweep: 24 ohm sample at the i-th sweep current."""
        g = self.settings.get
        if self.armed_mode != "PDEL" or g("SOUR:PDEL:SWE", "OFF").upper() != "ON":
            return 1.0e-6 + random.gauss(0, 2e-9)
        spac = g("SOUR:SWE:SPAC", "LIN").upper()
        if spac == "LIST":
            cur = self.current_list[i] if i < len(self.current_list) else 0.0
        elif spac == "LOG":
            a, b, n = float(g("SOUR:CURR:STAR", "1e-6")), float(g("SOUR:CURR:STOP", "1e-3")), int(g("SOUR:SWE:POIN", "2"))
            cur = a * (b / a) ** (i / max(n - 1, 1))
        else:
            cur = float(g("SOUR:CURR:STAR", "0")) + i * float(g("SOUR:CURR:STEP", "0"))
        return 24.0 * (cur - float(g("SOUR:PDEL:LOW", "0"))) + random.gauss(0, 2e-9)

    # -------- transport API --------
    def write(self, cmd: str) -> None:
        with self._lock:
            cmd = cmd.strip()
            self.writes.append(cmd)
            if self.verbose:
                print("MOCK WRITE", cmd, flush=True)
            up = cmd.upper()
            if not up.startswith(KNOWN_PREFIXES):
                self.errors.append(f'-113,"Undefined header;{cmd}"')
                return
            if up == "*CLS":
                self.errors.clear()
            elif up == "TRAC:CLE":
                self.buffer.clear()
            elif up.endswith(":ARM"):
                self.armed_mode = up.split(":")[1]
                if self.armed_mode == "PDEL" and float(self.settings.get("SOUR:PDEL:SDEL", "6e-5")) < 55e-6:
                    self.errors.append('414,"Src delay >= 55 us recommended"')
            elif up == "INIT:IMM":
                if self.armed_mode is None:
                    self.errors.append('-221,"Settings conflict; not armed"')
                else:
                    self.buffer.clear()
                    self.running_since = time.monotonic()
                    self.output_on = True
            elif up == "SOUR:WAVE:INIT":
                if self.armed_mode != "WAVE":
                    self.errors.append('-221,"Settings conflict; waveform not armed"')
                else:
                    self.output_on = True
            elif up == "SOUR:WAVE:ABOR":
                self.armed_mode = None if self.armed_mode == "WAVE" else self.armed_mode
                self.output_on = False
            elif up == "SOUR:SWE:ABOR":
                self._points()
                self.running_since = None
                self.armed_mode = None
                self.output_on = False
            elif up.startswith("OUTP "):
                self.output_on = up.endswith("ON")
            elif up.startswith("SOUR:LIST:CURR"):
                if not up.startswith("SOUR:LIST:CURR:APP"):
                    self.current_list = []
                self.current_list += [float(x) for x in cmd.split(" ", 1)[1].split(",")]
            elif up.startswith("SYST:COMM:SER:SEND"):
                inner = cmd.split('"')[1] if '"' in cmd else ""
                if inner.upper().startswith(":READ?"):
                    # ohmic 24 ohm sample when sourcing DC
                    i = float(self.settings.get("SOUR:CURR", "0")) if self.output_on else 0.0
                    self._serial_reply = f"{24.0 * i + random.gauss(0, 2e-9):+.8E}\n\n"
            elif " " in cmd:
                key, value = cmd.split(" ", 1)
                self.settings[key.upper()] = value

    def query(self, cmd: str) -> str:
        with self._lock:
            up = cmd.strip().upper()
            if up == "*IDN?":
                return "KEITHLEY INSTRUMENTS INC.,MODEL 6221,MOCK,A00"
            if up == "SOUR:DELT:NVPR?":
                return "1"
            if up == "SYST:ERR?":
                return self.errors.pop(0) if self.errors else '0,"No error"'
            if up == "TRAC:POIN:ACT?":
                return str(self._points())
            if up.endswith(":ARM?"):
                self._points()
                return "1" if self.armed_mode == up.split(":")[1] else "0"
            if up == "SENS:DATA:LAT?":
                self._points()
                return f"{self.buffer[-1][0]:+.9E}" if self.buffer else "+9.91E37"
            if up == "TRAC:DATA?":
                self._points()
                return ",".join(f"{r:+.9E},{t:.4f}" for r, t in self.buffer)
            if up == "SYST:COMM:SER:ENT?":
                reply, self._serial_reply = self._serial_reply, ""
                return reply
            if up.startswith(":READ?"):
                return f"{1e-6 + random.gauss(0, 1e-9):+.8E}"
            if up == "OUTP?":
                return "1" if self.output_on else "0"
            if up.endswith("?") and up[:-1] in self.settings:
                value = self.settings[up[:-1]].upper()
                return {"ON": "1", "OFF": "0", "GUAR": "GUAR", "OLOW": "OLOW"}.get(value, value)
            self.errors.append(f'-113,"Undefined header;{cmd}"')
            return ""
