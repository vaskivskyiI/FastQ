"""IOC: Zurich Instruments MFLI lock-in (LabOne via zhinst-toolkit) - the instrument's own
measurements and settings, nothing experiment-specific.

    python ioc_host/iocs/mfli_ioc.py --device dev1234 --labone-host 192.168.1.94 --demod-index 1 --prefix FASTQ:MFLI1:
    python ioc_host/iocs/mfli_ioc.py --mock --device sim --prefix TEST:MFLI1:      (simulated)

One demodulator (--demod-index) with its oscillator, signal output amplitude and the
instrument's inputs/outputs:
  * demod sample (X.SCAN = poll rate, default .2 s; FastPoll* with FastPollTarget X for
    faster than .1 s): X, Y (V RMS), R, Theta, DemodFreq, AuxIn1/2 - one sample each poll
  * demod: DemodEnable, TimeConstant, FilterOrder, Sinc, DataRate, Harmonic, Phase,
    DemodInput; PhaseAdjust (auto phase); oscillator Frequency; RefSource (internal / external
    reference, extrefs/<n>/enable) + RefLocked
  * signal input SigIn* (range, AC, differential, 50 Ohm, float, scaling, on) + autorange;
    current input CurrIn* (range, on, float) + autorange
  * signal output SigOut* (on, amplitude PEAK, amplitude enable, range, offset, 50 Ohm,
    differential, add aux in 1, overload)
  * aux outputs AuxOut1..4 (= offset; manual mode output voltage), AuxOutSelect/Value/Scale/
    Demod/LimitLow/LimitHigh 0..3
  * overload flags SigInOverload / CurrInOverload / SigOutClip (status/flags/binary)
Derived quantities (sample resistance through a series resistor, ...) belong to the
control host's derived-values service, not here (removed 2026-10-27: Resistance, SeriesR).

Node names and enumerations: MFLI user manual, node documentation
(https://docs.zhinst.com/mfli_user_manual/nodedoc.html).

Safety: excitation (SigOut on/amplitude/offset/enable/add), aux outputs (AuxOut1 drives the
laser energy in the transport setup; the others may drive other instruments) and their source/
scale/limits are "protected"; settings that change a running measurement "confirm";
filter, rate and ranges "safe".

Notes: zhinst-core must match the LabOne server version (major.minor).
The MFLI's data server has very few client slots - this IOC should be the
only program with a LabOne session; everything else uses these PVs.
Nothing is written at startup (a disabled demod only logs an error unless
--enable-demod is given). All LabOne calls go through the instrument lock:
concurrent zhinst calls from several threads wedged the IOC mid-ramp (2026-09-27).
"""
from __future__ import annotations

import logging
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np  # noqa: E402

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402

log = logging.getLogger("mfli_ioc")

ORDERS = tuple(f"{6 * n} dB/oct" for n in range(1, 9))                   # demods/n/order 1..8
INPUTS = {"Sig In 1": 0, "Curr In 1": 1, "Trigger 1": 2, "Trigger 2": 3, "Aux Out 1": 4, "Aux Out 2": 5,
          "Aux Out 3": 6, "Aux Out 4": 7, "Aux In 1": 8, "Aux In 2": 9}  # demods/n/adcselect
AUX_SOURCES = {"Manual": -1, "Demod X": 0, "Demod Y": 1, "Demod R": 2, "Demod Theta": 3, "PID": 5,
               "Boxcar": 6, "PID Shift": 9, "PID Error": 10, "TU Filtered": 11, "TU Output": 13}  # auxouts/n/outputselect
REF_MODES = ("Internal", "External")
FLAG_SIGIN, FLAG_CURRIN, FLAG_SIGOUT = 1 << 0, 1 << 1, (1 << 5) | (1 << 6)  # status/flags/binary


def scalar(value) -> float:
    """zhinst readings may be bare numbers, 0-d or 1-element arrays (or enum members)."""
    arr = np.asarray(value)
    if arr.ndim == 0:
        return float(arr)
    return float(arr.reshape(-1)[-1]) if arr.size else math.nan


def ival(value) -> int:
    return int(round(scalar(value)))


def _node(path: str, dtype=float, **kw) -> Param:
    """A param read from (and, with set=True, written to) a node relative to an object of the
    instrument: path = "demod.order" -> self.demod.order()."""
    obj, name = path.split(".")
    conv = {float: scalar, int: ival, bool: lambda v: bool(ival(v))}[dtype]
    writable = kw.pop("set", False)
    setter = (lambda s, v: getattr(getattr(s, obj), name)(int(v) if dtype in (int, bool) else float(v))) \
        if writable else None
    return Param(dtype, get=lambda s: conv(getattr(getattr(s, obj), name)()), set=setter, **kw)


def _enum(path: str, mapping: dict, **kw) -> Param:
    """Enumerated node: choice name <-> node value."""
    obj, name = path.split(".")
    inverse = {v: k for k, v in mapping.items()}

    def get(s):
        v = ival(getattr(getattr(s, obj), name)())
        if v not in inverse:
            raise ValueError(f"{path} = {v}: not one of {sorted(inverse)}")
        return inverse[v]

    return Param("enum", choices=tuple(mapping), get=get,
                 set=lambda s, v: getattr(getattr(s, obj), name)(mapping[v]), **kw)


def _aux(n: int, field: str, dtype=float, **kw) -> Param:
    conv = {float: scalar, int: ival}[dtype]
    kw.setdefault("poll", 5.0)
    kw.setdefault("kind", "config")
    setter = None
    if kw.pop("set", True):
        def setter(s, v):
            getattr(s.drv.auxouts[n], field)(dtype(v))
    return Param(dtype, get=lambda s: conv(getattr(s.drv.auxouts[n], field)()), set=setter, **kw)


def _aux_out(n: int, kind: str) -> Param:
    return Param(float, units="V", precision=6, limits=(-10.0, 10.0), poll=1.0, kind=kind, safety="protected",
                 get=lambda s: scalar(s.drv.auxouts[n].offset()), set=lambda s, v: s.drv.auxouts[n].offset(v),
                 doc=f"aux output {n + 1} offset (= its voltage in Manual mode)")


def _aux_select(n: int) -> Param:
    def get(s):
        v = ival(s.drv.auxouts[n].outputselect())
        return next(k for k, c in AUX_SOURCES.items() if c == v)
    return Param("enum", choices=tuple(AUX_SOURCES), poll=5.0, kind="config", safety="protected", get=get,
                 set=lambda s, v: s.drv.auxouts[n].outputselect(AUX_SOURCES[v]),
                 doc=f"aux output {n + 1} signal (Manual = AuxOut{n + 1} offset only)")


class MFLI(Instrument):
    TYPE = "mfli"
    DESCRIPTION = "Zurich Instruments MFLI lock-in amplifier (LabOne): demod X/Y/R/theta, inputs, outputs, aux I/O"
    DEFAULT_PREFIX = "FASTQ:MFLI1:"
    POLL = 0.2

    # ---- demod sample (one sample per poll)
    X = Param(float, units="V", precision=9, get="read_sample", kind="hinted", overview=True, trend=True,
              doc="demod X (RMS)")
    Y = Param(float, units="V", precision=9, get=lambda s: s._sample[1], kind="hinted", doc="demod Y (RMS)")
    R = Param(float, units="V", precision=9, get=lambda s: math.hypot(*s._sample[:2]), overview=True,
              doc="demod amplitude sqrt(X^2+Y^2) (RMS)")
    Theta = Param(float, units="deg", precision=3, get=lambda s: math.degrees(math.atan2(s._sample[1], s._sample[0])),
                  doc="demod phase atan2(Y, X)")
    DemodFreq = Param(float, units="Hz", precision=4, get=lambda s: s._sample[4],
                      doc="demodulation frequency of the sample (oscillator x harmonic; external reference: tracked)")
    AuxIn1 = Param(float, units="V", precision=6, get=lambda s: s._sample[2], doc="aux input 1 (sample)")
    AuxIn2 = Param(float, units="V", precision=6, get=lambda s: s._sample[3], doc="aux input 2 (sample)")

    # ---- demodulator
    DemodEnable = _node("demod.enable", bool, set=True, poll=1.0, kind="config", safety="confirm",
                        doc="demodulator on (data streaming)")
    TimeConstant = Param(float, units="s", precision=6, limits=(1e-7, 500.0), poll=1.0, kind="config", safety="safe",
                      get=lambda s: scalar(s.demod.timeconstant()), set=lambda s, v: s.demod.timeconstant(v),
                      doc="low-pass filter time constant")
    FilterOrder = _enum("demod.order", {o: i + 1 for i, o in enumerate(ORDERS)}, poll=5.0, kind="config",
                        safety="safe", doc="low-pass filter order (roll-off)")
    Sinc = _node("demod.sinc", bool, set=True, poll=5.0, kind="config", safety="safe",
                 doc="sinc filter (for low frequencies)")
    DataRate = _node("demod.rate", float, set=True, units="Sa/s", precision=2, limits=(1e-3, 1e6), poll=5.0,
                     kind="config", safety="safe", doc="demod sample rate (the instrument picks the nearest)")
    Harmonic = _node("demod.harmonic", int, set=True, limits=(1, 1023), poll=5.0, kind="config", safety="confirm",
                     doc="demodulation at this multiple of the oscillator frequency")
    Phase = _node("demod.phaseshift", float, set=True, units="deg", precision=3, limits=(-360.0, 360.0),
                       poll=2.0, kind="config", safety="confirm", doc="reference phase shift")
    DemodInput = _enum("demod.adcselect", INPUTS, poll=5.0, kind="config", safety="confirm",
                       doc="signal demodulated by this demod")
    Frequency = Param(float, units="Hz", precision=4, limits=(0.0, 5e6), poll=1.0, kind="config", safety="confirm",
                    get=lambda s: scalar(s.osc.freq()), set=lambda s, v: s.osc.freq(v),
                    doc="oscillator frequency (internal reference)")
    RefSource = Param("enum", choices=REF_MODES, poll=2.0, kind="config", safety="confirm",
                    get=lambda s: REF_MODES[bool(ival(s.extref.enable()))],
                    set=lambda s, v: s.extref.enable(REF_MODES.index(v)),
                    doc="reference: Internal oscillator / External (extref tracks the reference input)")
    RefLocked = _node("extref.locked", bool, poll=2.0, kind="config", doc="external reference locked")

    # ---- signal input / current input
    SigInRange = _node("sigin.range", float, set=True, units="V", precision=4, limits=(1e-3, 3.0), poll=2.0,
                       kind="config", safety="safe", doc="voltage input range (next higher available)")
    SigInAC = _node("sigin.ac", bool, set=True, poll=5.0, kind="config", doc="AC coupling (else DC)")
    SigInDiff = _node("sigin.diff", bool, set=True, poll=5.0, kind="config", doc="differential input (A-B)")
    SigIn50Ohm = _node("sigin.imp50", bool, set=True, poll=5.0, kind="config", doc="50 Ohm input (else 10 MOhm)")
    SigInFloat = _node("sigin.float", bool, set=True, poll=5.0, kind="config", doc="floating input (else grounded)")
    SigInScaling = _node("sigin.scaling", float, set=True, precision=4, poll=5.0, kind="config",
                         doc="scale factor applied to the voltage input")
    SigInOn = _node("sigin.on", bool, set=True, poll=5.0, kind="config", doc="voltage input enabled")
    CurrInRange = _node("currin.range", float, set=True, units="A", precision=9, limits=(1e-9, 0.01), poll=2.0,
                        kind="config", safety="safe", doc="current input range (next higher available)")
    CurrInOn = _node("currin.on", bool, set=True, poll=5.0, kind="config", doc="current input enabled")
    CurrInFloat = _node("currin.float", bool, set=True, poll=5.0, kind="config", doc="floating current input")
    SigInOverload = Param(bool, poll=1.0, get=lambda s: s.flag(FLAG_SIGIN), overview=True,
                          doc="voltage input overload (status flag)")
    CurrInOverload = Param(bool, poll=1.0, get=lambda s: s.flag(FLAG_CURRIN), doc="current input overload")

    # ---- signal output (excitation)
    SigOutOn = _node("sigout.on", bool, set=True, poll=1.0, kind="config", safety="protected", overview=True,
                     doc="signal output on")
    Amplitude = Param(float, units="V", precision=6, limits=(0.0, 10.0), poll=1.0, kind="config", safety="protected",
                      get=lambda s: scalar(s.amp()), set=lambda s, v: s.amp(v), overview=True,
                      doc="excitation amplitude of this demod's oscillator (PEAK)")
    AmplitudeEnable = Param(bool, poll=2.0, kind="config", safety="protected",
                            get=lambda s: bool(ival(s.amp_enable())), set=lambda s, v: s.amp_enable(int(v)),
                            doc="this demod's amplitude added to the signal output")
    SigOutRange = _node("sigout.range", float, set=True, units="V", precision=3, limits=(0.0, 10.0), poll=5.0,
                        kind="config", doc="output range (next higher available; limits the amplitude)")
    SigOutOffset = _node("sigout.offset", float, set=True, units="V", precision=6, limits=(-10.0, 10.0), poll=2.0,
                         kind="config", safety="protected", doc="DC offset of the signal output")
    SigOut50Ohm = _node("sigout.imp50", bool, set=True, poll=5.0, kind="config",
                        doc="output into 50 Ohm (else high impedance)")
    SigOutDiff = _node("sigout.diff", bool, set=True, poll=5.0, kind="config", doc="differential output")
    SigOutAdd = _node("sigout.add", bool, set=True, poll=5.0, kind="config", safety="protected",
                      doc="add aux input 1 to the output")
    SigOutClip = Param(bool, poll=1.0, get=lambda s: s.flag(FLAG_SIGOUT), doc="signal output clipping (status flag)")

    # ---- aux outputs (AuxOut<n> = offset; numbered as on the front panel)
    AuxOut1 = _aux_out(0, "normal")
    AuxOut2 = _aux_out(1, "config")
    AuxOut3 = _aux_out(2, "config")
    AuxOut4 = _aux_out(3, "config")
    for _n in range(4):   # PV numbers = front panel Aux Output 1..4 (LabOne nodes 0..3)
        locals()[f"AuxOutSelect{_n + 1}"] = _aux_select(_n)
        locals()[f"AuxOutValue{_n + 1}"] = _aux(_n, "value", set=False, units="V", precision=6, poll=1.0, kind="normal",
                                            doc=f"voltage present on aux output {_n + 1}")
        locals()[f"AuxOutScale{_n + 1}"] = _aux(_n, "scale", precision=4, safety="protected",
                                            doc=f"aux output {_n + 1}: (signal + preoffset) * scale + offset")
        locals()[f"AuxOutDemod{_n + 1}"] = _aux(_n, "demodselect", int, limits=(0, 3), safety="protected",
                                            doc=f"aux output {_n + 1}: demod of the selected signal (0-based)")
        locals()[f"AuxOutLimitLow{_n + 1}"] = _aux(_n, "limitlower", units="V", precision=3, limits=(-10.0, 10.0),
                                               safety="protected", doc=f"aux output {_n + 1} lower clipping limit")
        locals()[f"AuxOutLimitHigh{_n + 1}"] = _aux(_n, "limitupper", units="V", precision=3, limits=(-10.0, 10.0),
                                                safety="protected", doc=f"aux output {_n + 1} upper clipping limit")
    del _n

    PhaseAdjust = Action("phase_adjust", safety="confirm", refresh=["Phase", "X", "Y", "R", "Theta"],
                         doc="auto phase: set Phase so that Theta reads 0")
    SigInAutorange = Action(lambda s: s.sigin.autorange(1), safety="safe", refresh=["SigInRange"],
                            doc="voltage input autorange (about 2x the input amplitude)")
    CurrInAutorange = Action(lambda s: s.currin.autorange(1), safety="safe", refresh=["CurrInRange"],
                             doc="current input autorange")

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--device", help="device id, e.g. dev1234")
        parser.add_argument("--labone-host", default="localhost")
        parser.add_argument("--labone-port", type=int, default=8004)
        parser.add_argument("--demod-index", type=int, default=0)
        parser.add_argument("--osc-index", type=int, default=0)
        parser.add_argument("--sigout-index", type=int, default=0)
        parser.add_argument("--amp-index", type=int, default=None,
                            help="sigouts/<n>/amplitudes index (default: the demod index)")
        parser.add_argument("--extref-index", type=int, default=0)
        parser.add_argument("--enable-demod", action="store_true", help="switch the demod on if it is off")

    def __init__(self, args) -> None:
        super().__init__(args)
        self._sample = (math.nan,) * 5
        self._flags_ok = True

    def open(self) -> None:
        a = self.args
        if self.mock:
            from ioc_host.drivers.mfli_sim import MockMFLIDevice

            self.drv = MockMFLIDevice()
        else:
            if not a.device:
                raise ValueError("--device is required (or --mock)")
            from zhinst.toolkit import Session

            self.drv = Session(a.labone_host, a.labone_port).connect_device(a.device)
        amp_index = a.demod_index if a.amp_index is None else a.amp_index
        self.demod = self.drv.demods[a.demod_index]
        self.osc = self.drv.oscs[a.osc_index]
        self.sigout = self.drv.sigouts[a.sigout_index]
        self.sigin = self.drv.sigins[0]
        self.currin = self.drv.currins[0]
        self.extref = self.drv.extrefs[a.extref_index]
        self.amp = self.sigout.amplitudes[amp_index]
        self.amp_enable = self.sigout.enables[amp_index]
        self._flags_ok = True
        if not scalar(self.demod.enable()):
            if a.enable_demod:
                self.demod.enable(True)
            else:
                log.error("demod %d is disabled - X/Y will not update (enable it, or use --enable-demod)",
                          a.demod_index)

    def close(self) -> None:
        pass  # the toolkit session has no close; a reconnect makes a new one

    def read_sample(self) -> float:
        s = self.demod.sample()
        self._sample = (scalar(s["x"]), scalar(s["y"]), scalar(s["auxin0"]), scalar(s["auxin1"]),
                        scalar(s["frequency"]))
        return self._sample[0]

    def flag(self, mask: int):
        """status/flags/binary bits; None (no update) if this firmware has no such node."""
        if not self._flags_ok:
            return None
        try:
            return bool(ival(self.drv.status.flags.binary()) & mask)
        except (AttributeError, KeyError, RuntimeError) as exc:
            self._flags_ok = False
            log.warning("status/flags/binary not readable (%s) - overload flags disabled", exc)
            return None

    def phase_adjust(self) -> None:
        self.demod.phaseadjust(1)


if __name__ == "__main__":
    run_instrument(MFLI)
