"""Simulated MFLI for `iocs/mfli_ioc.py --mock` - mimics the zhinst-toolkit
node objects the IOC touches (callable nodes: node() reads, node(v) writes).

Physics, only so run-layer tests see plausible numbers: two-terminal
measurement, excitation = sigout amplitude (peak) through a 6.7 MOhm series
resistor into a ~1 kOhm sample; X = V across the sample (RMS), rotated by the
demod phase shift; nothing when the output, the amplitude or the demod is off.
Setting AuxOut1 (node 0, the laser's external energy control during an energy ramp)
nudges the sample resistance, so a ramp is visible in the recorded data.
Aux outputs follow Value = (Signal + Preoffset) * Scale + Offset, clipped to
the limits; the input range sets the overload flag; autorange picks ~2x the
signal; phaseadjust zeroes theta.
"""
from __future__ import annotations

import math
import random
import time

import numpy as np

SERIES_OHM = 6.7e6
SIGIN_RANGES = (1e-3, 3e-3, 10e-3, 30e-3, 100e-3, 300e-3, 1.0, 3.0)
CURRIN_RANGES = (1e-9, 10e-9, 100e-9, 1e-6, 10e-6, 100e-6, 1e-3, 10e-3)
SIGOUT_RANGES = (0.01, 0.1, 1.0, 10.0)
SAMPLE_PHASE_DEG = 7.0  # cable / sample phase that phaseadjust removes


def _next_range(ranges, v):
    return next((r for r in ranges if r >= abs(v) * (1 - 1e-9)), ranges[-1])


class Node:
    def __init__(self, value=0.0, on_set=None):
        self.value = value
        self._on_set = on_set

    def __call__(self, *args):
        if args:
            self.value = self._on_set(args[0]) if self._on_set else args[0]
            return None
        return self.value


class Computed:
    """Read-only node whose value is computed."""

    def __init__(self, func):
        self._func = func

    def __call__(self, *args):
        if args:
            raise RuntimeError("read-only node")
        return self._func()


class _Indexed:
    def __init__(self, factory, n):
        self._items = [factory(i) for i in range(n)]

    def __getitem__(self, i):
        return self._items[i]

    def __len__(self):
        return len(self._items)


class _AuxOut:
    SOURCES = {0: "x", 1: "y", 2: "r", 3: "theta"}

    def __init__(self, device):
        self._dev = device
        self.offset = Node(0.0)
        self.outputselect = Node(-1)
        self.demodselect = Node(0)
        self.scale = Node(1.0)
        self.preoffset = Node(0.0)
        self.limitlower = Node(-10.0)
        self.limitupper = Node(10.0)
        self.value = Computed(self._value)

    def _value(self):
        sel = int(self.outputselect())
        signal = 0.0
        if sel in self.SOURCES:
            x, y = self._dev.demods[int(self.demodselect())]._xy()
            signal = {"x": x, "y": y, "r": math.hypot(x, y), "theta": math.degrees(math.atan2(y, x))}[self.SOURCES[sel]]
        v = (signal + self.preoffset()) * self.scale() + self.offset()
        return min(max(v, self.limitlower()), self.limitupper())


class _Osc:
    def __init__(self, i):
        self.freq = Node(213.35456)


class _SigOut:
    def __init__(self, i):
        self.on = Node(1)
        self.range = Node(10.0, lambda v: _next_range(SIGOUT_RANGES, v))
        self.offset = Node(0.0)
        self.imp50 = Node(0)
        self.diff = Node(0)
        self.add = Node(0)
        self.amplitudes = _Indexed(lambda j: Node(6.7), 4)  # peak V -> 1 uA through 6.7 MOhm
        self.enables = _Indexed(lambda j: Node(1), 4)
        self.over = Computed(lambda: int(self._peak() > self.range()))

    def _peak(self):
        # simplification: the largest enabled amplitude (the real output adds them)
        return abs(self.offset()) + max((self.amplitudes[j]() for j in range(4) if self.enables[j]()), default=0.0)


class _SigIn:
    def __init__(self, device):
        self._dev = device
        self.range = Node(1.0, lambda v: _next_range(SIGIN_RANGES, v))
        self.ac, self.diff, self.imp50, self.float = Node(0), Node(0), Node(0), Node(0)
        self.scaling = Node(1.0)
        self.on = Node(1)
        self.autorange = Node(0, self._autorange)

    def _autorange(self, v):
        if v:
            peak = max((math.hypot(*d._xy()) * math.sqrt(2) for d in self._dev.demods), default=0.0)
            self.range(2 * peak)
        return 0


class _CurrIn:
    def __init__(self):
        self.range = Node(1e-6, lambda v: _next_range(CURRIN_RANGES, v))
        self.on = Node(0)
        self.float = Node(0)
        self.scaling = Node(1.0)
        self.autorange = Node(0, lambda v: 0)


class _ExtRef:
    def __init__(self, i):
        self.enable = Node(0)
        self.demodselect = Node(1)
        self.adcselect = Node(0)
        self.automode = Node(4)
        self.locked = Computed(lambda: int(bool(self.enable())))


class _Demod:
    def __init__(self, device, index):
        self._dev = device
        self._index = index
        self.enable = Node(1)
        self.timeconstant = Node(0.0204)
        self.order = Node(4)
        self.rate = Node(1674.0)
        self.sinc = Node(0)
        self.harmonic = Node(1)
        self.phaseshift = Node(0.0)
        self.adcselect = Node(0)
        self.oscselect = Node(0)
        self.phaseadjust = Node(0, self._phaseadjust)

    def _phaseadjust(self, v):
        if v:
            self.phaseshift(SAMPLE_PHASE_DEG)
        return 0

    def _xy(self):
        d = self._dev
        out = d.sigouts[0]
        if not (self.enable() and out.on() and out.enables[self._index]()):
            return random.gauss(0, 1e-9), random.gauss(0, 1e-9)
        i_rms = (out.amplitudes[self._index]() / math.sqrt(2)) / SERIES_OHM
        # resistance: base + slow drift + response to AuxOut1 = node 0 (energy ramp)
        r = 1000.0 + 5.0 * math.sin(time.time() / 60.0) + 40.0 * d.auxouts[0].offset() + random.gauss(0, 0.2)
        v = i_rms * r * d.sigins[0].scaling()
        th = math.radians(SAMPLE_PHASE_DEG - self.phaseshift())
        return v * math.cos(th) + random.gauss(0, 1e-9), v * math.sin(th) + random.gauss(0, 1e-9)

    def sample(self):
        d = self._dev
        x, y = self._xy()
        return {
            "timestamp": np.array([int(time.time() * 60e6)], dtype=np.uint64),
            "x": np.array([x]),
            "y": np.array([y]),
            "frequency": np.array([d.oscs[int(self.oscselect())].freq() * self.harmonic()]),
            "phase": np.array([0.0]),
            "auxin0": np.array([random.gauss(0, 1e-4)]),
            "auxin1": np.array([random.gauss(0, 1e-4)]),
        }


class _Flags:
    def __init__(self, device):
        self._dev = device
        self.binary = Computed(self._binary)

    def _binary(self):
        d = self._dev
        peak = max(math.hypot(*dm._xy()) * math.sqrt(2) for dm in d.demods)
        return int(peak > d.sigins[0].range()) | (int(bool(d.sigouts[0].over())) << 5)


class _Status:
    def __init__(self, device):
        self.flags = _Flags(device)


class MockMFLIDevice:
    def __init__(self):
        self.auxouts = _Indexed(lambda i: _AuxOut(self), 4)
        self.oscs = _Indexed(_Osc, 1)
        self.sigouts = _Indexed(_SigOut, 1)
        self.sigins = _Indexed(lambda i: _SigIn(self), 1)
        self.currins = _Indexed(lambda i: _CurrIn(), 1)
        self.extrefs = _Indexed(_ExtRef, 2)
        self.demods = _Indexed(lambda i: _Demod(self, i), 4)
        self.status = _Status(self)
