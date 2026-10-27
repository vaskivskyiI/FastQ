r"""IOC: Keithley 6221 current source + 2182A nanovoltmeter, built around
MEASUREMENTS, not monitoring: set parameters -> Acquire -> read the results.
Nothing talks to the instrument while idle (no polling), and nothing is
written to it at startup.

    python ioc_host/iocs/keithley_ioc.py --resource GPIB0::18::INSTR --prefix FASTQ:KEITHLEY1:
    python ioc_host/iocs/keithley_ioc.py --mock --prefix TEST:KEITHLEY1:     # no hardware

Options: --k2182-resource GPIB0::7::INSTR if the 2182A has its own GPIB address
(only used for DC mode + NPLC); otherwise the 6221's RS-232 pass-through is used.
--setup-command "OUTP:RESP SLOW" (repeatable) is sent at the start of EVERY run.

Usage: write Mode, Compliance and the parameters of that mode, then put Acquire 1
WITH completion - it completes when the run has ended (State Done / Aborted / Error;
a second Acquire meanwhile is refused). Results: Mean, StdDev, N, Readings,
Timestamps (+ Currents for the sweeps). Progress while running: State,
PointsDone / PointsTotal, ElapsedS, LastReading. Abort 1 stops a run at any time
(and puts the source in a safe state when idle); Stop 1 ends a continuous run
(DeltaCount 0) as Done.

Modes: Delta, PulseDelta, DiffCond, DC (DcCurrent, DcReadings), DCSweep (CurrentList,
DcSettleS first point, DcPointSettleS), PulseSweep (TransportMeasure "Sweep Pulse":
PsSpacing Linear/Log/List, PsStart/PsStop/PsStep/PsPoints or CurrentList, PsReverse,
pulse shape from Pd*), SinglePulse (SpAmplitude, SpWidth: one pulse, one reading),
Wave (6221 sine/square/ramp current for WaveDurationS or until Stop - no readings).
Currents = the source current of each reading for the sweeps and DiffCond.

Output configuration per run (RangeMode/CurrentRange, OutputLow, InnerShield) and 2182A
settings (NPLC, VoltRange/VoltAutoRange, K2182Channel, Autozero) are sent at Acquire only
when not "Leave"/0. ReadStatus (refused during a run) queries the 6221's output state,
source current, compliance and range; they are read once after connect and after each run.

Safety levels: run parameters only take effect at Acquire (which asks for confirmation),
so they are "safe" - except Compliance ("protected": a higher compliance lets more voltage
reach the sample) and CAB ("confirm": switching compliance abort off).

Each Acquire: SOUR:SWE:ABOR, *CLS, TRAC:CLE, FORM:ELEM READ,TST, compliance,
--setup-commands, mode parameters -> SYST:ERR? -> ARM -> SYST:ERR? -> INIT:IMM
(DC: OUTP ON; Wave: SOUR:WAVE:ARM/INIT) -> poll TRAC:POIN:ACT? every PollPeriodS -> TRAC:DATA? ->
[SOUR:WAVE:ABOR +] SOUR:SWE:ABOR + OUTP OFF (always, also on error/abort/timeout).
"""
from __future__ import annotations

import logging
import math
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402
from ioc_host.drivers.keithley6221 import (  # noqa: E402
    BUFFER_MAX, K2182_CHANNELS, MODES, ON_OFF, OUTPUT_LOWS, RANGE_MODES, SHIELDS, SPACINGS, WAVE_FUNCTIONS,
    WAVE_RANGING, Keithley6221, RunParams)

log = logging.getLogger("keithley_ioc")
STATES = ("Idle", "Configuring", "Armed", "Running", "Done", "Aborted", "Error")
PARTIAL_PUBLISH_S = 0.5   # DC sweeps: publish the points measured so far this often


class RunAborted(Exception):
    pass


def _cfg(dtype=float, initial=None, units="", precision=9, safety="safe", **kw) -> Param:
    """A run parameter: kept by the IOC, sent to the instrument at Acquire."""
    return Param(dtype, writable=True, initial=initial, units=units, precision=precision, kind="config",
                 safety=safety, **kw)


def _result(dtype=float, **kw) -> Param:
    return Param(dtype, writable=False, initial=[0.0] if dtype == "array" else None, **kw)


class Keithley(Instrument):
    TYPE = "keithley"
    DESCRIPTION = "Keithley 6221 current source + 2182A nanovoltmeter (delta, pulse delta, DC, I-V sweeps)"
    DEFAULT_PREFIX = "FASTQ:KEITHLEY1:"

    # ---- status (read once after connect) / progress (published during a run)
    Idn = Param(str, poll=0, get=lambda s: s.drv.identify(), kind="config", doc="*IDN? of the 6221")
    NanovoltmeterPresent = Param(bool, poll=0, get=lambda s: s.drv.nanovoltmeter_present(), kind="config",
                                 doc="2182A answers")
    State = _result("enum", choices=STATES, overview=True, event="Error",
                    doc="Idle/Configuring/Armed/Running/Done/Aborted/Error")
    Message = _result(str, kind="config", overview=True, doc="what the last run did / why it failed")
    PointsDone = _result(int, kind="omitted", doc="points measured in this run")
    PointsTotal = _result(int, kind="omitted", doc="points expected (0 = continuous)")
    ElapsedS = _result(float, units="s", precision=1, kind="omitted", doc="run time so far")
    LastReading = _result(float, units="V", precision=12, kind="omitted", trend=True, doc="latest reading (live)")
    RunStartTime = _result(str, kind="omitted", doc="start of the last run")
    # instrument state (queries; read after connect, after each run and on ReadStatus - never polled)
    OutputOn = Param(bool, poll=0, get=lambda s: s.drv.output_on(), overview=True, doc="6221 output on (OUTP?)")
    SourceCurrent = Param(float, units="A", precision=9, poll=0, get=lambda s: s.drv.source_current(),
                          kind="config", doc="programmed DC level (SOUR:CURR?)")
    SourceCompliance = Param(float, units="V", precision=3, poll=0, get=lambda s: s.drv.compliance(),
                             kind="config", doc="compliance set in the 6221 (SOUR:CURR:COMP?)")
    SourceRange = Param(float, units="A", precision=9, poll=0, get=lambda s: s.drv.source_range(), kind="config",
                        doc="source range (SOUR:CURR:RANG?)")
    SourceRangeAuto = Param(bool, poll=0, get=lambda s: s.drv.source_range_auto(), kind="config",
                            doc="source autorange (SOUR:CURR:RANG:AUTO?)")
    OutputLowGrounded = Param(bool, poll=0, get=lambda s: s.drv.output_low_grounded(), kind="config",
                              doc="output low connected to earth (OUTP:LTE?)")
    ShieldGuard = Param(bool, poll=0, get=lambda s: s.drv.shield_guard(), kind="config",
                        doc="triax inner shield on guard (else output low; OUTP:ISH?)")

    # ---- results
    Mean = _result(float, units="V", precision=12, kind="hinted", overview=True, doc="mean of the readings")
    StdDev = _result(float, units="V", precision=12, doc="standard deviation of the readings")
    N = _result(int, doc="number of readings")
    Readings = _result("array", nelm=BUFFER_MAX, units="V", precision=12, doc="readings of the last run")
    Timestamps = _result("array", nelm=BUFFER_MAX, units="s", precision=6, doc="time of each reading")
    Currents = _result("array", nelm=BUFFER_MAX, units="A", precision=9, doc="sweeps: source current per reading")

    # ---- common parameters
    Mode = _cfg("enum", choices=MODES, doc="measurement mode")
    Compliance = _cfg(units="V", precision=3, limits=(0.0, 105.0), safety="protected", doc="voltage compliance (> 0)")
    CAB = _cfg(bool, initial=1, safety="confirm", doc="abort on compliance (Delta, DiffCond)")
    RangeMode = _cfg("enum", choices=RANGE_MODES, doc="6221 source range: Leave / Auto / Fixed (CurrentRange)")
    CurrentRange = _cfg(units="A", limits=(0.0, 0.105), doc="fixed source range (RangeMode Fixed)")
    OutputLow = _cfg("enum", choices=OUTPUT_LOWS, doc="output low terminal: Leave / Ground / Float (OUTP:LTE)")
    InnerShield = _cfg("enum", choices=SHIELDS, doc="triax inner shield: Leave / Output Low / Guard (OUTP:ISH)")
    NPLC = _cfg(precision=2, limits=(0.0, 60.0), doc="2182A integration (0 = leave as is)")
    VoltRange = _cfg(units="V", precision=3, limits=(0.0, 100.0), doc="2182A range (0 = leave as is)")
    VoltAutoRange = _cfg(bool, doc="2182A autorange (when VoltRange is 0)")
    K2182Channel = _cfg("enum", choices=K2182_CHANNELS, doc="2182A input channel for DC / DCSweep")
    Autozero = _cfg("enum", choices=ON_OFF, doc="2182A autozero (Off = faster, drifts)")
    TimeoutS = _cfg(units="s", precision=1, doc="run timeout (0 = automatic)")
    PollPeriodS = _cfg(units="s", precision=2, initial=0.25, limits=(0.05, 10.0), doc="progress poll period")
    # after SOUR:xxx:ARM the 6221 configures the 2182A over RS-232; INIT:IMM sent too early is
    # accepted but no reading ever arrives (real pair, 2026-09-26: 0 s fails, 0.5 s works)
    ArmSettleS = _cfg(units="s", precision=2, initial=1.0, limits=(0.0, 30.0), doc="wait between ARM and INIT")
    # Delta
    DeltaHigh = _cfg(units="A", initial=1e-6, limits=(-0.105, 0.105), doc="delta high current")
    DeltaLow = _cfg(units="A", initial=-1e-6, limits=(-0.105, 0.105), doc="delta low current")
    DeltaDelay = _cfg(units="s", precision=4, initial=0.02, doc="delta delay")
    DeltaCount = _cfg(int, initial=100, limits=(0, BUFFER_MAX), doc="readings (0 = continuous until Stop)")
    DeltaColdSwitch = _cfg(bool, doc="delta cold switching (SOUR:DELT:CSW)")
    # PulseDelta (+ pulse shape of PulseSweep)
    PdHigh = _cfg(units="A", initial=1e-3, limits=(-0.105, 0.105), doc="pulse high current")
    PdLow = _cfg(units="A", initial=0.0, limits=(-0.105, 0.105), doc="pulse low current")
    PdWidth = _cfg(units="s", precision=7, initial=110e-6, doc="pulse width")
    PdSourceDelay = _cfg(units="s", precision=7, initial=60e-6, doc="source delay")
    PdCount = _cfg(int, initial=100, limits=(1, BUFFER_MAX), doc="pulse-delta readings")
    PdIntervalPLC = _cfg(int, initial=5, limits=(1, 12000), doc="pulse interval (PLC)")
    PdLowMeas = _cfg(int, initial=2, limits=(1, 2), doc="low measurements per pulse (2 = 3-point, 1 = 2-point)")
    # DiffCond
    DconStart = _cfg(units="A", initial=-1e-5, limits=(-0.105, 0.105), doc="differential conductance start")
    DconStep = _cfg(units="A", initial=1e-6, doc="... step")
    DconStop = _cfg(units="A", initial=1e-5, limits=(-0.105, 0.105), doc="... stop")
    DconDelta = _cfg(units="A", initial=1e-6, doc="... delta")
    DconDelay = _cfg(units="s", precision=4, initial=0.02, doc="... delay")
    # DC, DCSweep
    DcCurrent = _cfg(units="A", limits=(-0.105, 0.105), doc="DC current")
    DcReadings = _cfg(int, initial=10, limits=(1, BUFFER_MAX), doc="DC readings")
    DcSettleS = _cfg(units="s", precision=2, initial=0.5, doc="settle before the first reading")
    DcPointSettleS = _cfg(units="s", precision=3, initial=0.01, doc="DCSweep: settle before each further point")
    CurrentList = _cfg("array", [0.0], nelm=BUFFER_MAX, units="A", doc="DCSweep / PulseSweep List: currents")
    # PulseSweep, SinglePulse (defaults = TransportMeasure GUI defaults)
    PsSpacing = _cfg("enum", choices=SPACINGS, doc="pulse sweep spacing")
    PsStart = _cfg(units="A", limits=(-0.105, 0.105), doc="pulse sweep start")
    PsStop = _cfg(units="A", initial=1e-3, limits=(-0.105, 0.105), doc="pulse sweep stop")
    PsStep = _cfg(units="A", initial=1e-4, doc="pulse sweep step (Linear)")
    PsPoints = _cfg(int, initial=11, limits=(1, BUFFER_MAX), doc="pulse sweep points (Log)")
    PsReverse = _cfg(bool, doc="sweep back to the start too")
    SpAmplitude = _cfg(units="A", initial=500e-6, limits=(-0.105, 0.105), doc="single pulse amplitude")
    SpWidth = _cfg(units="s", precision=7, initial=100e-6, doc="single pulse width")
    # Wave
    WaveFunction = _cfg("enum", choices=tuple(WAVE_FUNCTIONS), doc="waveform")
    WaveFrequency = _cfg(units="Hz", precision=4, initial=1000.0, limits=(1e-3, 1e5), doc="waveform frequency")
    WaveAmplitude = _cfg(units="A", initial=1e-6, limits=(2e-12, 0.105), doc="waveform amplitude (peak)")
    WaveOffset = _cfg(units="A", limits=(-0.105, 0.105), doc="waveform DC offset")
    WaveDurationS = _cfg(units="s", precision=1, limits=(0.0, 1e6), doc="waveform duration (0 = until Stop)")
    WaveRanging = _cfg("enum", choices=tuple(WAVE_RANGING), doc="waveform source ranging")

    Acquire = Action("acquire", exclusive=True, refresh=["OutputOn", "SourceCurrent"], timeout_s=7200.0,
                     safety="confirm", doc="one complete run in the configured Mode; completes when it ended")
    Abort = Action("abort", lock=False, safety="safe",
                   doc="abort the run (keeps the points so far); idle: source to safe state")
    Stop = Action("stop_continuous", lock=False, safety="safe",
                  doc="end a continuous run (Delta with DeltaCount 0, Wave without duration) as Done")
    ReadStatus = Action("read_status", exclusive=True, safety="safe", timeout_s=30.0,
                        doc="query the 6221 state (OutputOn, SourceCurrent, ...); refused during a run")

    ophyd = {"trigger": {"action": "Acquire", "timeout_s": 7200.0,
                         "require": {"param": "State", "values": ["Done"], "message": "Message"}},
             "stop": "Abort"}

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--resource", "--gpib", dest="resource", help="6221 VISA address, e.g. GPIB0::18::INSTR")
        parser.add_argument("--k2182-resource", help="2182A VISA address, if it has its own (else RS-232 pass-through)")
        parser.add_argument("--setup-command", action="append", default=[], dest="setup_commands",
                            help="raw SCPI sent at the start of every run, e.g. 'OUTP:RESP SLOW' (repeatable)")

    def __init__(self, args) -> None:
        super().__init__(args)
        self._abort = threading.Event()
        self._stop = threading.Event()
        self._running = False

    def open(self) -> None:
        if self.mock:
            from ioc_host.drivers.keithley6221_sim import MockK6221

            self.drv = Keithley6221(MockK6221(verbose=False))
            return
        if not self.args.resource:
            raise ValueError("--resource is required (or --mock)")
        k2182 = _open_visa(self.args.k2182_resource) if self.args.k2182_resource else None
        self.drv = Keithley6221(_open_visa(self.args.resource), k2182)

    def close(self) -> None:
        pass

    def on_exit(self) -> None:
        if self._running and self.drv is not None:  # IOC killed mid-run: leave the source safe
            self.drv.stop()

    # ------------------------------------------------------------------ run
    def _params(self) -> RunParams:
        v = self.values
        return RunParams(
            mode=v["Mode"], compliance_v=float(v["Compliance"]), cab=bool(v["CAB"]),
            delta_high=float(v["DeltaHigh"]), delta_low=float(v["DeltaLow"]), delta_delay=float(v["DeltaDelay"]),
            delta_count=int(v["DeltaCount"]),
            pd_high=float(v["PdHigh"]), pd_low=float(v["PdLow"]), pd_width=float(v["PdWidth"]),
            pd_source_delay=float(v["PdSourceDelay"]), pd_count=int(v["PdCount"]),
            pd_interval_plc=int(v["PdIntervalPLC"]), pd_low_meas=int(v["PdLowMeas"]),
            dcon_start=float(v["DconStart"]), dcon_step=float(v["DconStep"]), dcon_stop=float(v["DconStop"]),
            dcon_delta=float(v["DconDelta"]), dcon_delay=float(v["DconDelay"]),
            dc_current=float(v["DcCurrent"]), dc_readings=int(v["DcReadings"]), dc_settle_s=float(v["DcSettleS"]),
            dc_currents=[float(x) for x in v["CurrentList"]], dc_point_settle_s=float(v["DcPointSettleS"]),
            ps_spacing=v["PsSpacing"], ps_start=float(v["PsStart"]), ps_stop=float(v["PsStop"]),
            ps_step=float(v["PsStep"]), ps_points=int(v["PsPoints"]), ps_reverse=bool(v["PsReverse"]),
            sp_amplitude=float(v["SpAmplitude"]), sp_width=float(v["SpWidth"]),
            delta_cold_switch=bool(v["DeltaColdSwitch"]),
            wave_function=v["WaveFunction"], wave_frequency=float(v["WaveFrequency"]),
            wave_amplitude=float(v["WaveAmplitude"]), wave_offset=float(v["WaveOffset"]),
            wave_duration_s=float(v["WaveDurationS"]), wave_ranging=v["WaveRanging"],
            range_mode=v["RangeMode"], current_range=float(v["CurrentRange"]), output_low=v["OutputLow"],
            inner_shield=v["InnerShield"], nplc=float(v["NPLC"]), volt_range=float(v["VoltRange"]),
            volt_autorange=bool(v["VoltAutoRange"]), k2182_channel=v["K2182Channel"], autozero=v["Autozero"],
            extra_setup=list(self.args.setup_commands),
        )

    def _state(self, state: str, message: str | None = None) -> None:
        self.publish(State=state, **({} if message is None else {"Message": message[:255]}))

    def _results(self, readings: list, stamps: list, currents: list | None = None) -> None:
        n = len(readings)
        mean = sum(readings) / n if n else math.nan
        sd = math.sqrt(sum((r - mean) ** 2 for r in readings) / (n - 1)) if n > 1 else (0.0 if n else math.nan)
        if currents is not None:  # arrays first, N last: N rows are always there
            self.publish(Currents=currents or [0.0])
        self.publish(Readings=readings or [0.0], Timestamps=stamps or [0.0], Mean=mean, StdDev=sd, N=n)

    def _sleep(self, seconds: float) -> None:
        if self._abort.wait(seconds):
            raise RunAborted()

    def acquire(self) -> None:
        self._abort.clear()
        self._stop.clear()
        self._running = True
        self._partial: tuple[list, list] = ([], [])
        self._partial_currents: list[float] = []
        self._sweep: list[float] = []
        mode = None
        try:
            # the previous run's results go FIRST: nothing stale is read as this run's data
            self._results([], [], [])
            p = self._params()
            p.validate()
            mode = p.mode
            self._run(p)
        except RunAborted:
            log.warning("run aborted")
            n = self._salvage(mode)
            self._state("Aborted", f"aborted by user - kept {n} points")
        except Exception as exc:  # the put completes; State tells the client what happened
            log.exception("run failed")
            self._state("Error", f"{type(exc).__name__}: {exc}")
        finally:
            self._running = False
            try:
                self.drv.stop()
            except Exception:
                log.exception("stop() failed - CHECK INSTRUMENT OUTPUT STATE")
                self.publish(Message="stop() failed - check instrument output!")

    def _run(self, p: RunParams) -> None:
        k = self.drv
        self._sweep = p.sweep_currents()
        total = p.expected_points()
        timeout = float(self.values["TimeoutS"]) or (2 * p.estimated_duration_s() + 30)
        if total == 0:
            timeout = math.inf  # continuous delta / wave: runs until Stop/Abort (or the wave duration)
        poll = max(float(self.values["PollPeriodS"]), 0.05)
        self.publish(LastReading=math.nan, PointsDone=0, PointsTotal=total, ElapsedS=0.0,
                     RunStartTime=time.strftime("%Y-%m-%dT%H:%M:%S"))
        log.info("run: %s, %d points, timeout %.0f s", p.mode, total, timeout)
        self._state("Configuring", f"{p.mode}: configuring")
        k.configure(p)  # configure/arm: ~1-2 s of GPIB; an abort here never reaches INIT/OUTP ON
        if self._abort.is_set():
            raise RunAborted()
        k.arm(p)
        if self._abort.is_set():
            raise RunAborted()
        self._state("Armed")
        if p.mode not in ("DC", "DCSweep", "Wave"):
            self._sleep(max(float(self.values["ArmSettleS"]), 0.0))
        if k.warnings:
            log.warning("instrument warnings: %s", k.warnings)
        t0 = time.monotonic()
        k.start(p)
        self._state("Running", f"{p.mode}: running, {total} points")
        early = False
        if p.mode == "Wave":
            self._state("Running", f"Wave: {p.wave_function} {p.wave_amplitude:g} A, {p.wave_frequency:g} Hz")
            while not self._stop.is_set():
                self._sleep(poll)
                elapsed = time.monotonic() - t0
                self.publish(ElapsedS=elapsed)
                if p.wave_duration_s and elapsed >= p.wave_duration_s:
                    break
            self._state("Done", f"Wave: {time.monotonic() - t0:.1f} s")
            return
        if p.mode in ("DC", "DCSweep"):
            readings, stamps = self._partial
            currents = self._partial_currents
            sweep = p.mode == "DCSweep"
            last_pub = time.monotonic()
            if not sweep:
                self._sleep(p.dc_settle_s)
            for i, current in enumerate(p.dc_currents if sweep else [None] * total):
                if self._abort.is_set():
                    raise RunAborted()
                if sweep:
                    k.set_current(current)
                    self._sleep(p.dc_settle_s if i == 0 else p.dc_point_settle_s)
                    currents.append(current)
                readings.append(k.dc_read_one(p))
                stamps.append(time.monotonic() - t0)
                self.publish(LastReading=readings[-1], PointsDone=len(readings), ElapsedS=stamps[-1])
                if sweep and time.monotonic() - last_pub >= PARTIAL_PUBLISH_S:  # live data: the points so far
                    last_pub = time.monotonic()
                    self.publish(Currents=list(currents), Readings=list(readings), Timestamps=list(stamps),
                                 N=len(readings))
                if time.monotonic() - t0 > timeout:
                    raise TimeoutError(f"{p.mode} run exceeded {timeout:.0f} s")
            self._results(readings, stamps, currents if sweep else None)
        else:
            last, stalled, live = -1, 0, True  # live: SENS:DATA:LAT? - off if this firmware rejects it
            while True:
                self._sleep(poll)
                done = k.points_acquired()
                elapsed = time.monotonic() - t0
                self.publish(PointsDone=done, ElapsedS=elapsed)
                if live and done > 0:
                    try:
                        latest = k.latest_reading()
                        if latest is not None:
                            self.publish(LastReading=latest)
                    except Exception as exc:
                        live = False
                        log.warning("live reading disabled: %s", exc)
                if total == 0:
                    if self._stop.is_set():
                        break
                    continue
                if done >= total:
                    break
                if elapsed > timeout:
                    raise TimeoutError(f"{done}/{total} points after {timeout:.0f} s")
                stalled = stalled + 1 if done == last else 0  # stopped by itself (compliance abort)?
                last = done
                if stalled >= 3 and not k.is_armed(p.mode):
                    early = True
                    break
            readings, stamps = k.fetch()
            self._results(readings, stamps, self._sweep[:len(readings)] if self._sweep else None)
        if early:
            errors = k.check_errors()
            self._state("Error", f"instrument stopped early at {len(readings)}/{total} points"
                        + (": " + " | ".join(errors) if errors else " (compliance abort?)"))
        else:
            warn = f" (warning: {' | '.join(k.warnings)})" if k.warnings else ""
            self._state("Done", f"{p.mode}: {len(readings)} points in {time.monotonic() - t0:.1f} s{warn}")

    def _salvage(self, mode: str | None) -> int:
        """After an abort: publish whatever was measured so far."""
        try:
            self.drv.stop()  # stop the sweep first; the buffer is kept
            if mode == "Wave":
                return 0
            if mode in ("DC", "DCSweep", None):
                readings, stamps = self._partial
                self._results(readings, stamps, self._partial_currents if mode == "DCSweep" else None)
            else:
                readings, stamps = self.drv.fetch()
                self._results(readings, stamps, self._sweep[:len(readings)] if self._sweep else None)
            return len(readings)
        except Exception:
            log.exception("could not fetch partial data after abort")
            return 0

    def abort(self) -> None:
        if self._running:
            self._abort.set()  # the run stops and calls stop() itself
        else:
            if self.drv is None:
                raise ConnectionError("not connected")
            self.drv.stop()  # idle: nothing else talks to the instrument
            self._state("Idle", "abort while idle: source set to safe state")

    def stop_continuous(self) -> None:
        if self._running:
            self._stop.set()

    def read_status(self) -> None:
        """Nothing to do here: the IOC re-reads the status params after an action."""


def _open_visa(resource: str):
    import pyvisa

    inst = pyvisa.ResourceManager().open_resource(resource)
    # Read up to GPIB EOI, not the first LF: the 6221's pass-through reply
    # (SYST:COMM:SER:ENT?) embeds the 2182A's own CR/LF, and stopping at
    # the first LF left a stray terminator in the output buffer -> the
    # next query read "" and the 6221 logged -410 "Query INTERRUPTED"
    # (found on the real pair 2026-09-26). Callers strip whitespace.
    inst.read_termination = None
    inst.write_termination = "\n"
    inst.timeout = 30000  # ms - TRAC:DATA? of a full buffer takes a while
    inst.clear()  # GPIB device clear: flushes stale I/O buffers only, no settings change
    return inst


if __name__ == "__main__":
    run_instrument(Keithley)
