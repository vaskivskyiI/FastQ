"""Keithley 6221 current source (+ 2182A nanovoltmeter) - measurement-
oriented driver: configure -> arm -> start -> poll progress -> fetch
buffer -> stop. Used by iocs/keithley_ioc.py.

Raw SCPI rather than PyMeasure's Keithley6221: PyMeasure only covers plain
delta mode (no pulse delta, no differential conductance, no progress
query), and the whole value of this driver is the sequencing below.

Modes
    Delta       6221 alternates +HIGH/LOW, 2182A measures each, 6221
                buffers one delta voltage per cycle.       SOUR:DELT:*
    PulseDelta  short pulses (width/source-delay/interval). SOUR:PDEL:*
    DiffCond    differential conductance staircase sweep.  SOUR:DCON:*
    DCSweep     I-V: step through a current list, one 2182A reading per
                current (settle between), output on for the whole list.
    DC          plain DC current + N 2182A readings.  2182A is read either
                on its own VISA address (--k2182-resource) or through the
                6221's RS-232 pass-through (SYST:COMM:SER:SEND/ENT?).
    PulseSweep  pulse delta sweep (TransportMeasure "Sweep Pulse" tab):
                one pulse per current of a linear / log / custom list
                staircase, one 2182A reading per pulse.  SOUR:PDEL:SWE ON
    SinglePulse one pulse of a given width and amplitude (TransportMeasure
                Settings tab): a one-point list pulse sweep, source delay
                = width - 34 us so the reading is at the end of the pulse.
    Wave        6221 waveform generator (sine / square / ramp current, e.g.
                the excitation of an external lock-in) for a duration or
                until Stop; no readings.                   SOUR:WAVE:*

Output configuration sent at the start of a run when not "Leave" (as
TransportMeasure did before every measurement): source range (auto / fixed:
SOUR:CURR:RANG[:AUTO] - TransportMeasure fixed it before delta runs "to
prevent large fluctuations in the current after arming"), output-low
terminal (OUTP:LTE), triax inner shield (OUTP:ISH OLOW|GUAR); 2182A input
channel / autozero / autorange (DC modes, pass-through).

SAFETY
  * Nothing is written by __init__/identify()/nanovoltmeter_present() -
    queries only.
  * Every run ends with stop(): SOUR:SWE:ABOR + OUTP OFF, also on error
    or abort (callers use try/finally).
  * Compliance must be given explicitly for every run.

SCPI names follow the Keithley 6221 Reference Manual (Delta, Pulse Delta
and Differential Conductance chapter) and PyMeasure's keithley6221.py /
keithley2182.py (OUTP:ISH, OUTP:LTE, SOUR:CURR:RANG:AUTO, SOUR:DELT:CSW,
SOUR:WAVE:*, :SENS:CHAN, :SYST:AZER:STAT). check_errors() reads SYST:ERR?
after configuration, so a command this firmware doesn't accept surfaces as
a clear error before the source is ever started.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

MODES = ("Delta", "PulseDelta", "DiffCond", "DC", "DCSweep", "PulseSweep", "SinglePulse", "Wave")
SPACINGS = ("Linear", "Log", "List")  # PulseSweep: SOUR:SWE:SPAC LIN | LOG | LIST
RANGE_MODES = ("Leave", "Auto", "Fixed")         # SOUR:CURR:RANG:AUTO 1 | SOUR:CURR:RANG <A>
OUTPUT_LOWS = ("Leave", "Ground", "Float")       # OUTP:LTE ON | OFF
SHIELDS = ("Leave", "Output Low", "Guard")       # OUTP:ISH OLOW | GUAR
K2182_CHANNELS = ("Leave", "Ch1", "Ch2")         # :SENS:CHAN 1 | 2 (DC modes only)
ON_OFF = ("Leave", "On", "Off")
WAVE_FUNCTIONS = {"Sine": "SIN", "Square": "SQU", "Ramp": "RAMP"}
WAVE_RANGING = {"Best": "BEST", "Fixed": "FIX"}
PULSE_KEYS = {"PulseDelta": "PDEL", "PulseSweep": "PDEL", "SinglePulse": "PDEL"}
ARM_KEYS = {"Delta": "DELT", "DiffCond": "DCON", **PULSE_KEYS}
SP_DELAY_OFFSET_S = 34e-6  # TransportMeasure single pulse: source delay = width - 34 us
MAX_CURRENT_A = 0.105  # 6221 source limit
BUFFER_MAX = 65536


class Transport(Protocol):
    def write(self, cmd: str) -> Any: ...
    def query(self, cmd: str) -> str: ...


@dataclass
class RunParams:
    mode: str
    compliance_v: float
    cab: bool = True                 # compliance abort (delta/diffcond)
    # Delta
    delta_high: float = 1e-6
    delta_low: float = -1e-6
    delta_delay: float = 0.02
    delta_count: int = 100            # 0 = continuous (SOUR:DELT:COUN INF) until Stop
    # Pulse delta
    pd_high: float = 1e-3
    pd_low: float = 0.0
    pd_width: float = 110e-6
    pd_source_delay: float = 60e-6   # 6221 warns below 55 us (414)
    pd_count: int = 100
    pd_interval_plc: int = 5
    pd_low_meas: int = 2
    # Differential conductance
    dcon_start: float = -1e-5
    dcon_step: float = 1e-6
    dcon_stop: float = 1e-5
    dcon_delta: float = 1e-6
    dcon_delay: float = 0.02
    # DC
    dc_current: float = 0.0
    dc_readings: int = 10
    dc_settle_s: float = 0.5
    # DC sweep (I-V)
    dc_currents: list[float] = field(default_factory=list)
    dc_point_settle_s: float = 0.01
    # Pulse delta sweep (uses pd_width, pd_source_delay, pd_low, pd_low_meas, pd_interval_plc;
    # List spacing uses dc_currents)
    ps_spacing: str = "Linear"
    ps_start: float = 0.0
    ps_stop: float = 1e-3
    ps_step: float = 1e-4
    ps_points: int = 11               # Log spacing
    ps_reverse: bool = False          # sweep stop -> start (list: reversed)
    # Single pulse
    sp_amplitude: float = 1e-3
    sp_width: float = 500e-6
    # Delta cold switching (output relay opened between readings)
    delta_cold_switch: bool = False
    # Wave
    wave_function: str = "Sine"
    wave_frequency: float = 1000.0
    wave_amplitude: float = 1e-6     # A peak
    wave_offset: float = 0.0
    wave_duration_s: float = 0.0     # 0 = until Stop
    wave_ranging: str = "Best"
    # 6221 output configuration ("Leave" = don't send)
    range_mode: str = "Leave"
    current_range: float = 0.0
    output_low: str = "Leave"
    inner_shield: str = "Leave"
    # 2182A (0 / "Leave" = leave instrument setting unchanged)
    nplc: float = 0.0
    volt_range: float = 0.0
    volt_autorange: bool = False
    k2182_channel: str = "Leave"
    autozero: str = "Leave"
    extra_setup: list[str] = field(default_factory=list)

    def sweep_currents(self) -> list[float]:
        """Currents of a PulseSweep / SinglePulse / DiffCond staircase, in sourcing order."""
        if self.mode == "SinglePulse":
            return [self.sp_amplitude]
        if self.mode == "DiffCond":
            if self.dcon_step == 0:
                return []
            step = math.copysign(abs(self.dcon_step), self.dcon_stop - self.dcon_start)
            return [self.dcon_start + k * step for k in range(self.expected_points())]
        if self.mode != "PulseSweep":
            return []
        if self.ps_spacing == "List":
            out = list(self.dc_currents)
        elif self.ps_spacing == "Log":
            n, a, b = self.ps_points, self.ps_start, self.ps_stop
            if n < 2 or a * b <= 0:
                return [a] if n == 1 else []
            out = [a * (b / a) ** (i / (n - 1)) for i in range(n)]
        else:  # Linear, like the 6221: start + k*step while not past stop
            if self.ps_step == 0:
                return []
            step = math.copysign(abs(self.ps_step), self.ps_stop - self.ps_start)
            n = int(math.floor(abs(self.ps_stop - self.ps_start) / abs(step) + 1e-9)) + 1
            out = [self.ps_start + k * step for k in range(n)]
        return out[::-1] if self.ps_reverse else out

    def expected_points(self) -> int:
        if self.mode == "Delta":
            return self.delta_count  # 0 = continuous
        if self.mode == "Wave":
            return 0  # no readings
        if self.mode == "DCSweep":
            return len(self.dc_currents)
        if self.mode in ("PulseSweep", "SinglePulse"):
            return len(self.sweep_currents())
        if self.mode == "PulseDelta":
            return self.pd_count
        if self.mode == "DiffCond":
            if self.dcon_step == 0:
                return 0
            return int(math.floor(abs(self.dcon_stop - self.dcon_start) / abs(self.dcon_step) + 1e-9)) + 1
        return self.dc_readings

    def estimated_duration_s(self) -> float:
        """Rough upper estimate, used for the default run timeout."""
        nplc_s = max(self.nplc, 5.0) / 50.0
        n = self.expected_points()
        if self.mode == "Wave":
            return self.wave_duration_s
        if self.mode == "Delta":
            per = 2 * (self.delta_delay + nplc_s)
        elif self.mode in ("PulseDelta", "PulseSweep"):
            per = 3 * max(self.pd_interval_plc, 5) / 50.0
        elif self.mode == "SinglePulse":
            per = 2.0
        elif self.mode == "DiffCond":
            per = 2 * (self.dcon_delay + nplc_s)
        elif self.mode == "DCSweep":
            per = self.dc_point_settle_s + nplc_s + 0.7  # pass-through read ~0.6 s
        else:
            per = nplc_s + 0.2
            n += 1
        return n * per

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"Unknown mode {self.mode!r} (one of {MODES})")
        if not self.compliance_v > 0:
            raise ValueError("Compliance (V) must be set > 0 before a run")
        if self.mode == "PulseSweep":
            if self.ps_spacing not in SPACINGS:
                raise ValueError(f"PulseSweep spacing {self.ps_spacing!r} not in {SPACINGS}")
            if self.ps_spacing == "Log" and (self.ps_start * self.ps_stop <= 0 or self.ps_points < 2):
                raise ValueError("Log sweep: start and stop non-zero with the same sign, >= 2 points")
            if self.pd_low_meas not in (1, 2):  # TransportMeasure offers 1 or 2 (6221 manual: LME 1|2)
                raise ValueError("PulseSweep low measurements must be 1 or 2")
        n = self.expected_points()
        continuous = self.mode == "Wave" or (self.mode == "Delta" and n == 0)
        if not continuous and not 1 <= n <= BUFFER_MAX:
            raise ValueError(f"{self.mode}: {n} points requested, must be 1..{BUFFER_MAX}")
        currents = self.dc_currents if self.mode == "DCSweep" else [self.dc_current] if self.mode == "DC" else []
        currents = currents or self.sweep_currents()
        if self.mode == "DiffCond":
            currents = [c + s * self.dcon_delta for c in currents for s in (-1, 1)]
        if self.mode == "Wave":
            currents = [abs(self.wave_offset) + abs(self.wave_amplitude)]
        currents = currents + {"Delta": [self.delta_high, self.delta_low], "PulseDelta": [self.pd_high, self.pd_low],
                               "PulseSweep": [self.pd_low]}.get(self.mode, [])
        if any(abs(c) > MAX_CURRENT_A for c in currents):
            raise ValueError(f"current above the 6221 limit of {MAX_CURRENT_A} A")
        if self.mode == "Delta" and self.delta_delay < 0:
            raise ValueError("Delta delay must be >= 0")
        # 3-point (2 low readings) or 2-point (1 low reading) pulse delta; TransportMeasure offered 2 | 1.
        # The former check here (2 | 3) refused the 2-point mode and allowed a value the 6221 rejects.
        if self.mode == "PulseDelta" and self.pd_low_meas not in (1, 2):
            raise ValueError("PulseDelta low measurements must be 1 or 2")
        if self.range_mode not in RANGE_MODES or self.output_low not in OUTPUT_LOWS \
                or self.inner_shield not in SHIELDS or self.k2182_channel not in K2182_CHANNELS \
                or self.autozero not in ON_OFF:
            raise ValueError("invalid output / 2182A configuration choice")
        if self.range_mode == "Fixed":
            if not 0 < self.current_range <= MAX_CURRENT_A:
                raise ValueError("Fixed source range: CurrentRange must be in (0, 0.105] A")
            if any(abs(c) > self.current_range * 1.05 for c in currents):  # 6221 ranges allow 105 %
                raise ValueError(f"a run current exceeds the fixed source range {self.current_range:g} A")
        if self.mode == "Wave":
            if self.wave_function not in WAVE_FUNCTIONS or self.wave_ranging not in WAVE_RANGING:
                raise ValueError(f"Wave function in {tuple(WAVE_FUNCTIONS)}, ranging in {tuple(WAVE_RANGING)}")
            if not 1e-3 <= self.wave_frequency <= 1e5:
                raise ValueError("Wave frequency must be 1 mHz .. 100 kHz")
            if not 2e-12 <= self.wave_amplitude <= MAX_CURRENT_A:
                raise ValueError("Wave amplitude must be 2 pA .. 105 mA (peak)")
            if self.wave_duration_s < 0:
                raise ValueError("Wave duration must be >= 0 (0 = until Stop)")
        if self.mode == "SinglePulse" and self.sp_width <= SP_DELAY_OFFSET_S:
            raise ValueError(f"single pulse width must be > {SP_DELAY_OFFSET_S * 1e6:.0f} us")


def _fmt(x: float) -> str:
    return f"{x:.6E}"


_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def parse_numbers(text: str) -> list[float]:
    """Parse a comma-separated SCPI reply, tolerating unit suffixes
    ("1.23E-06VDC") some firmware appends."""
    out = []
    for part in text.strip().split(","):
        m = _NUM.search(part)
        if m:
            out.append(float(m.group(0)))
    return out


class Keithley6221:
    def __init__(self, transport: Transport, k2182: Transport | None = None) -> None:
        self.io = transport
        self.k2182 = k2182  # separate 2182A session, if it has its own address
        self.warnings: list[str] = []  # advisory messages from the last configure/arm
        self._wave = False  # waveform armed/running: stop() must abort it too

    # ---------------- queries only (safe at any time) ----------------

    def identify(self) -> str:
        return self.io.query("*IDN?").strip()

    def nanovoltmeter_present(self) -> bool:
        return self.io.query("SOUR:DELT:NVPR?").strip() in ("1", "+1")

    def points_acquired(self) -> int:
        return int(float(self.io.query("TRAC:POIN:ACT?")))

    def is_armed(self, mode: str) -> bool:
        key = ARM_KEYS.get(mode)
        if key is None:
            return False
        return self.io.query(f"SOUR:{key}:ARM?").strip() in ("1", "+1")

    def check_errors(self) -> list[str]:
        errors = []
        for _ in range(20):
            reply = self.io.query("SYST:ERR?").strip()
            if reply.startswith(("0", "+0")):
                break
            errors.append(reply)
        return errors

    def _raise_on_errors(self, context: str) -> None:
        """Advisory messages (e.g. 414 "Src delay >= 55 us recommended")
        are kept in self.warnings; anything else aborts the run."""
        fatal = []
        for e in self.check_errors():
            (self.warnings if "recommended" in e.lower() else fatal).append(e)
        if fatal:
            raise RuntimeError(f"{context}: " + " | ".join(fatal))

    def output_on(self) -> bool:
        return self.io.query("OUTP?").strip() in ("1", "+1", "ON")

    def source_current(self) -> float:
        return parse_numbers(self.io.query("SOUR:CURR?"))[0]

    def compliance(self) -> float:
        return parse_numbers(self.io.query("SOUR:CURR:COMP?"))[0]

    def source_range(self) -> float:
        return parse_numbers(self.io.query("SOUR:CURR:RANG?"))[0]

    def source_range_auto(self) -> bool:
        return self.io.query("SOUR:CURR:RANG:AUTO?").strip() in ("1", "+1", "ON")

    def output_low_grounded(self) -> bool:
        return self.io.query("OUTP:LTE?").strip() in ("1", "+1", "ON")

    def shield_guard(self) -> bool:
        return self.io.query("OUTP:ISH?").strip().upper().startswith("GUAR")

    def fetch(self) -> tuple[list[float], list[float]]:
        """Buffer as (readings, timestamps) - FORM:ELEM READ,TST pairs."""
        values = parse_numbers(self.io.query("TRAC:DATA?"))
        return values[0::2], values[1::2]

    # ---------------- 2182A ----------------

    def _send_2182(self, cmd: str) -> None:
        if self.k2182 is not None:
            self.k2182.write(cmd)
        else:
            self.io.write(f'SYST:COMM:SER:SEND "{cmd}"')

    def _query_2182(self, cmd: str, wait_s: float) -> str:
        """Query the 2182A. Through the 6221's RS-232 pass-through this is
        fiddly (verified on the real pair, 2026-09-26): ENT? returns
        whatever bytes have arrived, the reply's second line terminator
        comes back as a separate empty "line", and polling ENT? rapidly
        while the 2182A is still measuring can wedge the 6221
        (-410/-420 query errors). So: drain leftovers, send, wait, then a
        few spaced ENT? calls, skipping empty replies."""
        if self.k2182 is not None:
            return self.k2182.query(cmd)
        for _ in range(5):  # drain stale bytes / terminators
            if not self.io.query("SYST:COMM:SER:ENT?").strip():
                break
        self.io.write(f'SYST:COMM:SER:SEND "{cmd}"')
        time.sleep(wait_s)
        for _ in range(6):
            reply = self.io.query("SYST:COMM:SER:ENT?").strip()
            if reply:
                return reply
            time.sleep(0.3)
        return ""

    # ---------------- run sequencing (writes) ----------------

    def configure(self, p: RunParams) -> None:
        """Everything up to (not including) the start trigger. Output
        stays off. Raises on any SCPI error reported by the instrument."""
        p.validate()
        io = self.io
        io.write("SOUR:SWE:ABOR")
        io.write("*CLS")
        io.write("TRAC:CLE")
        io.write("FORM:ELEM READ,TST")
        io.write(f"SOUR:CURR:COMP {_fmt(p.compliance_v)}")
        if p.range_mode == "Auto":
            io.write("SOUR:CURR:RANG:AUTO 1")
        elif p.range_mode == "Fixed":
            io.write("SOUR:CURR:RANG:AUTO 0")
            io.write(f"SOUR:CURR:RANG {_fmt(p.current_range)}")
        if p.output_low != "Leave":
            io.write(f"OUTP:LTE {'ON' if p.output_low == 'Ground' else 'OFF'}")
        if p.inner_shield != "Leave":
            io.write(f"OUTP:ISH {'GUAR' if p.inner_shield == 'Guard' else 'OLOW'}")
        for cmd in p.extra_setup:
            io.write(cmd)
        if p.mode in ("DC", "DCSweep") and p.k2182_channel != "Leave":
            # delta modes: the 6221 runs the 2182A itself (channel 1)
            self._send_2182(f":SENS:CHAN {p.k2182_channel[-1]}")
        if p.autozero != "Leave":
            self._send_2182(f":SYST:AZER:STAT {'ON' if p.autozero == 'On' else 'OFF'}")
        if p.nplc > 0:
            self._send_2182(f":SENS:VOLT:NPLC {p.nplc:g}")
        if p.volt_range > 0:
            self._send_2182(f":SENS:VOLT:RANG {p.volt_range:g}")
        elif p.volt_autorange:
            self._send_2182(":SENS:VOLT:RANG:AUTO 1")

        n = p.expected_points()
        cab = "ON" if p.cab else "OFF"
        if p.mode == "Delta":
            io.write("UNIT V")
            io.write(f"SOUR:DELT:HIGH {_fmt(p.delta_high)}")
            io.write(f"SOUR:DELT:LOW {_fmt(p.delta_low)}")
            io.write(f"SOUR:DELT:DEL {_fmt(p.delta_delay)}")
            io.write(f"SOUR:DELT:COUN {n if n else 'INF'}")
            io.write("SOUR:SWE:COUN 1")
            io.write(f"SOUR:DELT:CAB {cab}")
            io.write(f"SOUR:DELT:CSW {'ON' if p.delta_cold_switch else 'OFF'}")
            io.write(f"TRAC:POIN {n if n else BUFFER_MAX}")
        elif p.mode == "PulseDelta":
            io.write("UNIT V")
            io.write(f"SOUR:PDEL:HIGH {_fmt(p.pd_high)}")
            io.write(f"SOUR:PDEL:LOW {_fmt(p.pd_low)}")
            io.write(f"SOUR:PDEL:WIDT {_fmt(p.pd_width)}")
            io.write(f"SOUR:PDEL:SDEL {_fmt(p.pd_source_delay)}")
            io.write(f"SOUR:PDEL:COUN {n}")
            io.write(f"SOUR:PDEL:INT {int(p.pd_interval_plc)}")
            io.write("SOUR:PDEL:SWE OFF")
            io.write(f"SOUR:PDEL:LME {int(p.pd_low_meas)}")
            io.write(f"TRAC:POIN {n}")
        elif p.mode in ("PulseSweep", "SinglePulse"):
            self._configure_pulse_sweep(p)
        elif p.mode == "DiffCond":
            io.write("UNIT V")
            io.write(f"SOUR:DCON:STAR {_fmt(p.dcon_start)}")
            io.write(f"SOUR:DCON:STEP {_fmt(p.dcon_step)}")
            io.write(f"SOUR:DCON:STOP {_fmt(p.dcon_stop)}")
            io.write(f"SOUR:DCON:DELT {_fmt(p.dcon_delta)}")
            io.write(f"SOUR:DCON:DEL {_fmt(p.dcon_delay)}")
            io.write(f"SOUR:DCON:CAB {cab}")
            io.write(f"TRAC:POIN {n}")
        elif p.mode == "DCSweep":  # start from 0; output switched on in start()
            io.write("SOUR:CURR 0")
        elif p.mode == "Wave":
            io.write(f"SOUR:WAVE:FUNC {WAVE_FUNCTIONS[p.wave_function]}")
            io.write(f"SOUR:WAVE:FREQ {_fmt(p.wave_frequency)}")
            io.write(f"SOUR:WAVE:AMPL {_fmt(p.wave_amplitude)}")
            io.write(f"SOUR:WAVE:OFFS {_fmt(p.wave_offset)}")
            io.write(f"SOUR:WAVE:RANG {WAVE_RANGING[p.wave_ranging]}")
            io.write("SOUR:WAVE:DUR:TIME " + (_fmt(p.wave_duration_s) if p.wave_duration_s > 0 else "INF"))
        else:  # DC - source value only; output switched on in start()
            io.write(f"SOUR:CURR {_fmt(p.dc_current)}")

        self.warnings = []
        self._raise_on_errors("Instrument rejected configuration")

    def _configure_pulse_sweep(self, p: RunParams) -> None:
        """Pulse delta sweep as TransportMeasure measure.sweep_sett() / settings.send_pulse()
        programs it. Linear/Log: start/stop/step (log: points), pulse interval via SOUR:DEL
        = PLC/50 s; List (and the single pulse): SOUR:LIST:CURR + :APP one value per command."""
        io, n = self.io, p.expected_points()
        currents = p.sweep_currents()
        if p.mode == "SinglePulse":
            width, sdel, lme = p.sp_width, p.sp_width - SP_DELAY_OFFSET_S, 2
        else:
            width, sdel, lme = p.pd_width, p.pd_source_delay, p.pd_low_meas
        io.write("UNIT V")
        io.write("SOUR:PDEL:SWE ON")
        io.write(f"SOUR:PDEL:LOW {_fmt(p.pd_low if p.mode == 'PulseSweep' else 0.0)}")
        io.write(f"SOUR:PDEL:WIDT {_fmt(width)}")
        io.write(f"SOUR:PDEL:SDEL {_fmt(sdel)}")
        io.write(f"SOUR:PDEL:LME {int(lme)}")
        if p.mode == "SinglePulse" or p.ps_spacing == "List":
            io.write("SOUR:SWE:SPAC LIST")
            io.write(f"SOUR:LIST:CURR {_fmt(currents[0])}")
            for c in currents[1:]:
                io.write(f"SOUR:LIST:CURR:APP {_fmt(c)}")
        else:
            io.write(f"SOUR:SWE:SPAC {'LOG' if p.ps_spacing == 'Log' else 'LIN'}")
            io.write(f"SOUR:CURR:STAR {_fmt(currents[0])}")
            io.write(f"SOUR:CURR:STOP {_fmt(currents[-1])}")
            if p.ps_spacing == "Log":
                io.write(f"SOUR:SWE:POIN {n}")
            else:
                io.write(f"SOUR:CURR:STEP {_fmt(currents[1] - currents[0] if n > 1 else abs(p.ps_step))}")
            io.write(f"SOUR:DEL {_fmt(p.pd_interval_plc / 50.0)}")
        io.write("SOUR:SWE:COUN 1")
        io.write(f"TRAC:POIN {n}")

    def arm(self, p: RunParams) -> None:
        key = "WAVE" if p.mode == "Wave" else ARM_KEYS.get(p.mode)
        if key is None:
            return  # DC has no arm step
        self._wave = p.mode == "Wave"
        self.io.write(f"SOUR:{key}:ARM")
        self._raise_on_errors(f"Arming {p.mode} failed")

    def start(self, p: RunParams) -> None:
        if p.mode in ("DC", "DCSweep"):
            self.io.write("OUTP ON")
        elif p.mode == "Wave":
            self.io.write("SOUR:WAVE:INIT")
        else:
            self.io.write("INIT:IMM")

    def set_current(self, current_a: float) -> None:
        if abs(current_a) > MAX_CURRENT_A:
            raise ValueError(f"{current_a} A above the 6221 limit")
        self.io.write(f"SOUR:CURR {_fmt(current_a)}")

    def latest_reading(self) -> float | None:
        """Latest delta/pulse-delta reading while a run is going (for live
        monitoring); None if the instrument has none yet."""
        values = parse_numbers(self.io.query("SENS:DATA:LAT?"))
        if not values or abs(values[0]) > 9e37:  # 9.91E37 = SCPI "no data"
            return None
        return values[0]

    def dc_read_one(self, p: RunParams) -> float:
        # 2182A default 5 NPLC + filter + 19.2k serial: ~0.6 s measured
        wait = max(0.6, max(p.nplc, 5.0) / 50.0 * 3)
        values = parse_numbers(self._query_2182(":READ?", wait))
        if not values:
            raise RuntimeError("2182A returned no reading")
        return values[0]

    def stop(self) -> None:
        """Safe state: abort any sweep, switch the output off, and leave no
        DC level programmed (so a later manual OUTP ON sources nothing)."""
        try:
            if self._wave:
                self.io.write("SOUR:WAVE:ABOR")
                self._wave = False
            self.io.write("SOUR:SWE:ABOR")
        finally:
            self.io.write("OUTP OFF")
            self.io.write("SOUR:CURR 0")
