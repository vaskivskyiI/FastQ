"""IOC: National Instruments DAQ analog input (NI USB/PCIe-6289) via NI-DAQmx (`nidaqmx` package).

    python ioc_host/iocs/ni_daq_ioc.py --channels Dev1/ai0,Dev1/ai1 --prefix FDM:DAQ:
    python ioc_host/iocs/ni_daq_ioc.py --mock --prefix TEST:DAQ:

A Bluesky detector: trigger() runs Acquire = one finite acquisition of
NSamples per channel at SampleRate (TerminalConfig, default differential, +-Range V),
optionally started by a trigger on TriggerSource: a digital edge (4dmag NI.py: /Dev1/PFI0,
rising) or an analog edge at TriggerLevel (4dmag fdm/daq.py configureTrigger: APFI0,
rising, 2 V - what the pump-probe scripts used). Results: Mean / Std per channel (arrays,
channel order), Mean0..Mean3 / Std0..Std3 as scalars for plotting, Waveform0..3 = the
samples of the first four channels (Waveform = channel 0, as before). Settings are kept
by the IOC (initial values from the command line) and used by the next Acquire:
a task is created per acquisition, as 4dmag fdm/daq.py did with configureInput
+ readInput; nothing touches the DAQ outside Acquire and the AnalogOut puts.
Lowpass On/Off sets the AI lowpass filter of every channel (4dmag lowPassFilter;
devices without a programmable filter reject it). Analog outputs (--ao-channels,
e.g. Dev1/ao0,Dev1/ao1): AnalogOut0..3 write one value each (on-demand task); the DAQ
holds it after the task ends; there is no hardware readback, the PV shows the last put.
API: NI-DAQmx Python (https://nidaqmx-python.readthedocs.io): add_ai_voltage_chan,
cfg_samp_clk_timing, cfg_dig_edge_start_trig, cfg_anlg_edge_start_trig, ai_lowpass_enable,
add_ao_voltage_chan. Needs the NI-DAQmx runtime.
Safety: acquisition settings "safe"; analog outputs "protected" (they may drive hardware).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402

MAX_CH, MAX_SAMPLES, MAX_AO = 32, 65536, 4
TRIGGER = ("Off", "Rising", "Falling", "Analog rising", "Analog falling")  # Rising/Falling = digital edge
TERMINALS = ("Diff", "RSE", "NRSE", "PseudoDiff")
LOWPASS = ("Leave", "On", "Off")


class Driver:
    def __init__(self, device: str) -> None:
        import nidaqmx.system

        names = [d.name for d in nidaqmx.system.System.local().devices]
        if device and device not in names:
            raise ConnectionError(f"DAQ device {device!r} not found; available: {names}")
        self.device = device

    def read(self, channels, rate, n, vrange, trigger, source, timeout_s, terminal="Diff", level=0.0,
             lowpass="Leave"):
        import nidaqmx
        from nidaqmx.constants import AcquisitionType, Edge, Slope, TerminalConfiguration

        tc = {"Diff": "DIFF", "RSE": "RSE", "NRSE": "NRSE", "PseudoDiff": "PSEUDO_DIFF"}[terminal]
        # older nidaqmx releases call it PSEUDODIFFERENTIAL
        tconf = getattr(TerminalConfiguration, tc, None) or getattr(TerminalConfiguration, "PSEUDODIFFERENTIAL")
        with nidaqmx.Task() as task:
            for ch in channels:
                chan = task.ai_channels.add_ai_voltage_chan(ch, terminal_config=tconf, min_val=-vrange,
                                                            max_val=vrange)
                if lowpass != "Leave":
                    chan.ai_lowpass_enable = lowpass == "On"
            task.timing.cfg_samp_clk_timing(rate, active_edge=Edge.RISING, sample_mode=AcquisitionType.FINITE,
                                            samps_per_chan=n)
            if trigger in ("Rising", "Falling"):
                task.triggers.start_trigger.cfg_dig_edge_start_trig(
                    source, trigger_edge=Edge.RISING if trigger == "Rising" else Edge.FALLING)
            elif trigger != "Off":
                task.triggers.start_trigger.cfg_anlg_edge_start_trig(
                    source, trigger_slope=Slope.RISING if trigger == "Analog rising" else Slope.FALLING,
                    trigger_level=level)
            data = task.read(number_of_samples_per_channel=n, timeout=timeout_s)
        return np.asarray(data, dtype=float).reshape(len(channels), n)

    def write_ao(self, channel: str, value: float) -> None:
        import nidaqmx

        with nidaqmx.Task() as task:
            task.ao_channels.add_ao_voltage_chan(channel, min_val=-10.0, max_val=10.0)
            task.write(float(value), auto_start=True)


class SimDriver:
    def __init__(self) -> None:
        self.rng = np.random.default_rng()

    def read(self, channels, rate, n, vrange, trigger, source, timeout_s, terminal="Diff", level=0.0,
             lowpass="Leave"):
        t = np.arange(n) / rate
        sig = [0.5 * (i + 1) + 0.1 * np.sin(2 * np.pi * 1000 * t) for i in range(len(channels))]
        noise = 0.003 if lowpass == "On" else 0.01
        return np.clip(np.array(sig) + self.rng.normal(0, noise, (len(channels), n)), -vrange, vrange)

    def write_ao(self, channel: str, value: float) -> None:
        pass


def _cfg(key: str):
    return lambda s: s.cfg[key]


def _set(key: str):
    return lambda s, v: s.cfg.__setitem__(key, v)


def _mean(i: int):
    return lambda s: s.mean[i] if i < len(s.mean) else float("nan")


def _std(i: int):
    return lambda s: s.std[i] if i < len(s.std) else float("nan")


def _wave(i: int):
    return lambda s: s.data[i] if i < len(s.data) else [0.0]


def _ao(i: int) -> Param:
    return Param(float, units="V", precision=4, limits=(-10.0, 10.0), set=lambda s, v: s.write_ao(i, v),
                 kind="config", safety="protected", doc=f"analog output {i} (--ao-channels; no readback)")


class NIDaq(Instrument):
    TYPE = "ni_daq"
    DESCRIPTION = "NI DAQ analog input (NI-6289, differential, finite acquisition per trigger)"
    DEFAULT_PREFIX = "FDM:DAQ:"
    ophyd = {"trigger": {"action": "Acquire", "timeout_s": 60}}

    Channels = Param(str, get=_cfg("channels"), set=_set("channels"), poll=0, kind="config", safety="safe",
                     doc="comma-separated physical channels, e.g. Dev1/ai0,Dev1/ai1")
    SampleRate = Param(float, units="Hz", precision=1, get=_cfg("rate"), set=_set("rate"), poll=0,
                       limits=(1.0, 500_000.0), kind="config", safety="safe",
                       doc="sample clock (NI-6289: 500 kS/s single channel, 625 kS/s aggregate)")
    NSamples = Param(int, get=_cfg("samples"), set=_set("samples"), poll=0, limits=(1, MAX_SAMPLES), kind="config",
                     safety="safe", doc="samples per channel per Acquire")
    Range = Param(float, units="V", precision=2, get=_cfg("range"), set=_set("range"), poll=0, limits=(0.1, 10.0),
                  kind="config", safety="safe", doc="input range +-Range")
    TerminalConfig = Param("enum", choices=TERMINALS, get=_cfg("terminal"), set=_set("terminal"), poll=0,
                           kind="config", safety="safe", doc="input terminal configuration (4dmag: Diff)")
    Lowpass = Param("enum", choices=LOWPASS, get=_cfg("lowpass"), set=_set("lowpass"), poll=0, kind="config",
                    safety="safe", doc="AI lowpass filter of the channels (Leave = device default)")
    Trigger = Param("enum", choices=TRIGGER, get=_cfg("trigger"), set=_set("trigger"), poll=0, kind="config",
                    safety="safe", doc="start trigger on TriggerSource: digital edge, analog edge at TriggerLevel, "
                                       "or Off (start immediately)")
    TriggerSource = Param(str, get=_cfg("source"), set=_set("source"), poll=0, kind="config", safety="safe",
                          doc="start trigger terminal (digital e.g. /Dev1/PFI0, analog e.g. APFI0)")
    TriggerLevel = Param(float, units="V", precision=3, get=_cfg("level"), set=_set("level"), poll=0,
                         limits=(-10.0, 10.0), kind="config", safety="safe", doc="analog trigger level")
    Timeout = Param(float, units="s", get=_cfg("timeout"), set=_set("timeout"), poll=0, limits=(0.1, 3600.0),
                    kind="config", safety="safe", doc="how long Acquire waits for the data (incl. the trigger)")

    Mean0 = Param(float, units="V", precision=6, get=_mean(0), poll=0, kind="hinted", overview=True, trend=True,
                  doc="mean of channel 0")
    Mean1 = Param(float, units="V", precision=6, get=_mean(1), poll=0, overview=True)
    Mean2 = Param(float, units="V", precision=6, get=_mean(2), poll=0)
    Mean3 = Param(float, units="V", precision=6, get=_mean(3), poll=0)
    Std0 = Param(float, units="V", precision=6, get=_std(0), poll=0, doc="standard deviation of channel 0")
    Std1 = Param(float, units="V", precision=6, get=_std(1), poll=0)
    Std2 = Param(float, units="V", precision=6, get=_std(2), poll=0)
    Std3 = Param(float, units="V", precision=6, get=_std(3), poll=0)
    Mean = Param("array", nelm=MAX_CH, units="V", precision=6, get=lambda s: s.mean, poll=0,
                 doc="mean per channel (order of Channels)")
    Std = Param("array", nelm=MAX_CH, units="V", precision=6, get=lambda s: s.std, poll=0,
                doc="standard deviation per channel")
    Waveform = Param("array", nelm=MAX_SAMPLES, units="V", precision=5, get=_wave(0), poll=0,
                     kind="omitted", doc="samples of the first channel")
    Waveform1 = Param("array", nelm=MAX_SAMPLES, units="V", precision=5, get=_wave(1), poll=0, kind="omitted",
                      doc="samples of channel 1")
    Waveform2 = Param("array", nelm=MAX_SAMPLES, units="V", precision=5, get=_wave(2), poll=0, kind="omitted",
                      doc="samples of channel 2")
    Waveform3 = Param("array", nelm=MAX_SAMPLES, units="V", precision=5, get=_wave(3), poll=0, kind="omitted",
                      doc="samples of channel 3")
    NAcquired = Param(int, get=lambda s: s.count, poll=0, doc="acquisitions done")
    AnalogOut0 = _ao(0)
    AnalogOut1 = _ao(1)
    AnalogOut2 = _ao(2)
    AnalogOut3 = _ao(3)

    # exclusive: a second Acquire is refused at once instead of queueing behind a long one
    Acquire = Action("acquire", doc="one acquisition (put completes with the data)", timeout_s=60, exclusive=True,
                     safety="safe", refresh=["Mean0", "Mean1", "Mean2", "Mean3", "Std0", "Std1", "Std2", "Std3",
                                             "Mean", "Std", "Waveform", "Waveform1", "Waveform2", "Waveform3",
                                             "NAcquired"])

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--device", default="", help="check this DAQ device is present at connect, e.g. Dev1")
        parser.add_argument("--channels", default="Dev1/ai0", help="initial Channels")
        parser.add_argument("--rate", type=float, default=10000.0, help="initial SampleRate (Hz)")
        parser.add_argument("--samples", type=int, default=1000, help="initial NSamples")
        parser.add_argument("--trigger", choices=TRIGGER, default="Off", help="initial Trigger")
        parser.add_argument("--trigger-source", default="/Dev1/PFI0", help="initial TriggerSource")
        parser.add_argument("--trigger-level", type=float, default=1.0, help="initial TriggerLevel (V, analog)")
        parser.add_argument("--terminal", choices=TERMINALS, default="Diff", help="initial TerminalConfig")
        parser.add_argument("--range", type=float, default=10.0, help="initial Range (V)")
        parser.add_argument("--ao-channels", default="", help="analog outputs for AnalogOut0..3, e.g. Dev1/ao0,Dev1/ao1")

    def __init__(self, args) -> None:
        super().__init__(args)
        # settings live here (not on the DAQ): the command line gives the values after a restart
        self.cfg = {"channels": args.channels, "rate": args.rate, "samples": args.samples, "range": args.range,
                    "trigger": args.trigger, "source": args.trigger_source, "timeout": 10.0, "level": args.trigger_level,
                    "terminal": args.terminal, "lowpass": "Leave"}
        self.ao = [c.strip() for c in args.ao_channels.split(",") if c.strip()][:MAX_AO]
        self.mean, self.std, self.data, self.count = np.zeros(0), np.zeros(0), np.zeros((0, 1)), 0

    def open(self) -> None:
        self.drv = SimDriver() if self.mock else Driver(self.args.device)

    def acquire(self) -> None:
        c = self.cfg
        channels = [ch.strip() for ch in c["channels"].split(",") if ch.strip()][:MAX_CH]
        if not channels:
            raise ValueError("Channels is empty")
        data = self.drv.read(channels, c["rate"], c["samples"], c["range"], c["trigger"], c["source"], c["timeout"],
                             c["terminal"], c["level"], c["lowpass"])
        self.mean, self.std, self.data = data.mean(axis=1), data.std(axis=1), data
        self.count += 1

    def write_ao(self, i: int, value: float) -> None:
        if i >= len(self.ao):
            raise ValueError(f"AnalogOut{i}: no channel configured (--ao-channels)")
        self.drv.write_ao(self.ao[i], value)


if __name__ == "__main__":
    run_instrument(NIDaq)
