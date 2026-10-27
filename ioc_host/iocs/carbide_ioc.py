"""IOC: Light Conversion CARBIDE laser (REST API v1, lab port 20010).

    python ioc_host/iocs/carbide_ioc.py --host 192.168.1.10 --prefix FASTQ:LASER1:
    python ioc_host/iocs/carbide_ioc.py --host 192.168.1.10 --read-only     (monitoring only)
    python ioc_host/iocs/carbide_ioc.py --mock --prefix TEST:LASER1:          (in-process laser model)

All fast readbacks come from GET /Basic + /Advanced/IsPpEnabled + /ExternalControl
per poll (ShutterOpen_RBV.SCAN, default .5 s); compressor, RA frequency, interlock
and optional sensors every 5 s; presets / serial / ranges once after connecting.
Setting ShutterOpen, Attenuator, PulseDuration, PPDivider, BurstCount or
CompressorPosition completes when the readback confirms it; PPEnable completes when
the laser accepted it (a finite burst switches the picker off by itself, often
before the readback ever shows "on").

Endpoints: CARBIDE REST API documentation v1 (/Basic, /StretcherCompressor,
/ExternalControl, /Advanced, /Chiller). The REST API cannot select a harmonic or
output directly - that is what the presets are for (ApplyPreset; Harmonic shows the
result). There is no "start/operate" endpoint either: ApplyPreset brings the laser
from standby to Operational (via Housekeeping), Standby sends it back. Endpoints a
laser does not have (404: harmonics power meter, chiller link) stay blank.

SAFETY: nothing is sent to the laser at startup or by polling (the first version
of this IOC wrote each readback back to the laser every scan and, via
bool("Off") == True, opened the shutter). --read-only makes the driver refuse
every non-GET request. Everything that lets light out or raises the output
(shutter, pulse picker, attenuator, divider, energy/trigger source, presets, RA
frequency) is "protected": one level per control, whatever the value.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402
from ioc_host.drivers.carbide import SYNC_CONFIGS, TRIGGER_SOURCES, issues_text  # noqa: E402

log = logging.getLogger("carbide_ioc")
ENERGY = ("Internal", "External")
SYNC_LABELS = ("Disabled", "PpOpensClosesAuto", "PpClosesOnFreqError")  # EPICS enum labels <= 25 characters
SLOW = 5.0


def _basic(key: str, cast=float, default=float("nan")):
    def get(s):
        v = s._status.get(key)
        return default if v is None else cast(v)
    return get


def _ext(key: str):
    return lambda s: s._external.get(key) or "Internal"


def _slow(key: str, cast=float, default=float("nan")):
    def get(s):
        v = s.slow().get(key)
        return default if v is None else cast(v)
    return get


def _range(d) -> str:
    return f"{d.get('From')} .. {d.get('To')}" if isinstance(d, dict) else ""


def _confirmed(tolerance: float, timeout_s: float) -> dict:
    return {"tolerance": tolerance, "settle_s": 0.0, "timeout_s": timeout_s}


class Carbide(Instrument):
    TYPE = "carbide"
    DESCRIPTION = "Light Conversion CARBIDE femtosecond laser (REST API)"
    DEFAULT_PREFIX = "FASTQ:LASER1:"
    POLL = 0.5

    # -- output (fast group: one GET /Basic for all of these)
    ShutterOpen = Param(bool, get="read_status", set="write_shutter", overview=True, safety="protected",
                        positioner=_confirmed(0.5, 15.0), doc="1 = output enabled (shutter open), 0 = closed")
    Attenuator = Param(float, units="%", precision=2, limits=(0.0, 100.0), overview=True, safety="protected",
                       get=_basic("ActualAttenuatorPercentage"), set=lambda s, v: s.drv.set_attenuator(v),
                       positioner=_confirmed(0.5, 60.0), doc="attenuator transmission (master attenuator)")
    # pulse duration / compressor: no change of pulse energy, so confirm (plans scan it)
    PulseDuration = Param(float, units="fs", precision=1, get=_basic("ActualPulseDuration"),
                          set=lambda s, v: s.drv.set_pulse_duration(v), positioner=_confirmed(5.0, 60.0),
                          doc="pulse duration (sign = chirp); valid ranges in PulseDurationRange")
    PPDivider = Param(int, limits=(1, 10_000_000), get=_basic("ActualPpDivider", int, 0), safety="protected",
                      set=lambda s, v: s.drv.set_pulse_picker_divider(v), positioner=_confirmed(0.5, 15.0),
                      doc="pulse-picker divider (rep rate = RA frequency / divider; lower = more power)")
    PPEnable = Param(bool, get=lambda s: bool(s._status.get("IsPpEnabled")), safety="protected",
                     set=lambda s, v: s.drv.set_pulse_picker_enabled(v),
                     doc="pulse picker on; a finite burst switches itself off when done")
    BurstCount = Param(int, limits=(0, 100_000_000), get=_basic("ActualBurstPulseCount", int, 0),
                       set=lambda s, v: s.drv.set_burst_pulse_count(v), positioner=_confirmed(0.5, 15.0),
                       doc="pulses per burst, 0 = free running")
    EnergyControl = Param("enum", choices=ENERGY, get=_ext("FastEnergyControlSource"), safety="protected",
                          set="write_energy_control", doc="Internal | External (energy follows the analog input)")
    OutputEnabled = Param(bool, kind="config", get=lambda s: bool(s._status.get("IsOutputEnabled")),
                          doc="laser output enabled")
    EmissionWarning = Param(bool, overview=True, get=lambda s: bool(s._status.get("IsEmissionWarningActive")),
                            doc="emission warning active (the laser may emit)")
    State = Param(str, kind="config", overview=True, get=lambda s: str(s._status.get("ActualStateName", ""))[:39],
                  doc="laser state: Initializing, StandingBy, GoingToStandby, Housekeeping, Operational, "
                      "Service, Failure ...")
    GeneralStatus = Param(str, kind="config", get=lambda s: str(s._status.get("GeneralStatus") or "")[:255],
                          doc="status message (free text, e.g. 'Waiting for powerlock..')")
    OutputFrequency = Param(float, units="kHz", precision=4, kind="config", get=_basic("ActualOutputFrequency"),
                            doc="output repetition rate (after the pulse picker)")
    OutputPower = Param(float, units="W", precision=4, get=_basic("ActualOutputPower"), trend=True,
                        doc="output power (calculated by the laser)")
    OutputEnergy = Param(float, units="uJ", precision=4, kind="config", get=_basic("ActualOutputEnergy"),
                         doc="pulse energy (calculated by the laser)")
    RaEnergy = Param(float, units="uJ", precision=3, kind="config", get=_basic("TargetRaEnergy"),
                     doc="target regenerative-amplifier pulse energy (calculated)")
    Harmonic = Param(int, kind="config", get=_basic("ActualHarmonic", int, 0),
                     doc="harmonic at the output (1 = fundamental); selected by the presets")
    Errors = Param(str, kind="config", event="change", get=lambda s: issues_text(s._status.get("Errors"))[:255],
                   doc="active errors (latched until laser start / preset execution)")
    Warnings = Param(str, kind="config", get=lambda s: issues_text(s._status.get("Warnings"))[:255],
                     doc="active warnings")
    LaserErrorCount = Param(int, kind="omitted", get=lambda s: len(s._status.get("Errors") or []),
                            doc="number of active laser errors")
    Powerlock = Param(bool, kind="config", get=lambda s: bool(s._status.get("IsPowerlockEnabled")),
                      set=lambda s, v: s.drv.post("/Basic/EnablePowerlock" if v else "/Basic/DisablePowerlock"),
                      doc="RA powerlock (output power stabilisation)")

    # -- presets (harmonic / rate / power configurations stored in the laser)
    PresetIndex = Param(int, limits=(0, 99), kind="config", get=_basic("SelectedPresetIndex", int, -1),
                        set=lambda s, v: s.drv.put("/Basic/SelectedPresetIndex", int(v)),
                        doc="selected preset (0-based; ApplyPreset executes it)")
    LastPreset = Param(int, kind="config", get=_basic("LastExecutedPresetIndex", int, -1),
                       doc="last executed preset")
    Presets = Param(str, poll=0, kind="config", max_length=2048, get="read_presets",
                    doc="presets stored in the laser: index: name")
    ApplyPreset = Action(lambda s: s.drv.post("/Basic/ApplySelectedPreset"), safety="protected", timeout_s=30.0,
                         doc="execute the selected preset (Housekeeping, then Operational; may change harmonic, "
                             "rate and power; also brings the laser out of standby)")
    Standby = Action(lambda s: s.drv.post("/Basic/GoToStandby"),
                     doc="send the laser to standby (stops emission; back with ApplyPreset)")

    # -- compressor (slow group)
    CompressorPosition = Param(float, units="mm", precision=4, poll=SLOW, kind="config",
                               get=_slow("CompressorPosition"), positioner=_confirmed(0.005, 60.0),
                               set=lambda s, v: s.drv.put("/StretcherCompressor/TargetPosition", v),
                               doc="compressor stage position (prefer PulseDuration)")
    CompressorRange = Param(str, poll=SLOW, kind="config", get=lambda s: s.slow().get("CompressorRange", ""),
                            doc="valid compressor positions, mm")
    PulseDurationRange = Param(str, poll=SLOW, kind="config", get=lambda s: s.slow().get("DurationRange", ""),
                               doc="valid pulse durations, fs: negative chirp ; positive chirp")
    RaFrequency = Param(float, units="kHz", precision=3, poll=SLOW, kind="config", safety="protected",
                        get=_slow("ActualRaFrequency"), set=lambda s, v: s.drv.put("/Advanced/TargetRaFrequency", v),
                        doc="RA repetition rate (before the picker); large changes need ~30 min to settle - "
                            "presets are the preferred way")
    HarmonicPower = Param(float, units="W", precision=4, poll=SLOW, get=_slow("HarmonicPower"),
                          doc="harmonics module power meter (blank if not installed / not measured)")
    RemoteInterlock = Param(bool, poll=SLOW, overview=True, get=lambda s: bool(s.slow().get("RemoteInterlock")),
                            doc="remote interlock active")
    ChillerTemp = Param(float, units="C", precision=2, poll=SLOW, kind="config", get=_slow("ChillerTemp"),
                        doc="chiller water temperature (blank without the RS-232 chiller link)")
    AttenuatorRange = Param(str, poll=0, kind="config",
                            get=lambda s: _range(s.drv.get_optional("/Advanced/AvailableAttenuatorPercentageRange")),
                            doc="range in which the attenuator can work, %")
    SerialNumber = Param(str, poll=0, kind="config", get=lambda s: str(s.drv.get_optional("/Basic/SerialNumber") or ""))
    FirmwareVersion = Param(str, poll=0, kind="config",
                            get=lambda s: str(s.drv.get_optional("/Basic/FirmwareVersion") or ""))

    # -- external control (fast group: GET /ExternalControl)
    TriggerSource = Param("enum", choices=TRIGGER_SOURCES, kind="config", get=_ext("AomTriggerSource"),
                          set=lambda s, v: s.drv.put("/ExternalControl/AomTriggerSource", v), safety="protected",
                          doc="pulse picker / AOM trigger: Internal or an external TTL (low / high active)")
    ExternalSync = Param("enum", choices=SYNC_LABELS, kind="config",
                         get=lambda s: SYNC_LABELS[SYNC_CONFIGS.index(s._external.get("ExternalSyncConfiguration")
                                                                      or "Disabled")],
                         set=lambda s, v: s.drv.put("/ExternalControl/ExternalSyncConfiguration",
                                                    SYNC_CONFIGS[SYNC_LABELS.index(v)]),
                         doc="lock the pulses to an external frequency generator: Disabled | PP opens and closes "
                             "automatically | PP closes on frequency error")
    AnalogControl = Param("enum", choices=ENERGY, kind="config", get=_ext("AnalogControlSource"), safety="protected",
                          set=lambda s, v: s.drv.put("/ExternalControl/AnalogControlSource", v),
                          doc="analog control voltage from the laser (Internal) or the external connector")
    HvEnergyControl = Param("enum", choices=ENERGY, kind="config", get=_ext("HvFastEnergyControlSource"),
                            set=lambda s, v: s.drv.put("/ExternalControl/HvFastEnergyControlSource", v),
                            safety="protected", doc="high-voltage fast energy control source (optional feature)")

    # closing needs no PIN: one-click "safe" counterparts of the protected ShutterOpen / PPEnable
    CloseOutput = Action(lambda s: s.drv.set_shutter_open(False), safety="safe",
                         doc="close the laser output (shutter)", refresh=["ShutterOpen", "OutputPower"])
    DisablePP = Action(lambda s: s.drv.set_pulse_picker_enabled(False), safety="safe",
                       doc="disable the pulse picker", refresh=["ShutterOpen", "PPEnable", "OutputPower"])
    ResetRemoteInterlock = Action(lambda s: s.drv.post("/Advanced/ResetRemoteInterlock"), safety="protected",
                                  doc="reset the remote interlock (re-arms the laser)")
    ReduceLeak = Action(lambda s: s.drv.post("/Advanced/ReduceLeak"), timeout_s=10.0,
                        doc="pulse-picker leak reduction (~2 s; run at target power after warm-up)")
    SaveShortestPulse = Action(lambda s: s.drv.post("/StretcherCompressor/SaveAsShortestImpulseDuration"),
                               safety="protected", doc="recalibrate: the current compressor position becomes "
                                                       "the shortest pulse")

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--host", help="laser IP address")
        parser.add_argument("--port", type=int, default=20010)
        parser.add_argument("--read-only", action="store_true", help="refuse every command to the laser")

    def __init__(self, args) -> None:
        super().__init__(args)
        self._status: dict = {}
        self._external: dict = {}
        self._slow: dict = {}
        self._slow_t = 0.0

    def open(self) -> None:
        from ioc_host.drivers.carbide import CarbideLaser, SimLaser

        if self.mock:
            self.drv = SimLaser("carbide", read_only=self.args.read_only)
            return
        if not self.args.host:
            raise ValueError("--host is required")
        self.drv = CarbideLaser(host=self.args.host, port=self.args.port, read_only=self.args.read_only)
        self.drv.connect()  # one GET
        log.info("Carbide at %s:%s%s", self.args.host, self.args.port, " (READ-ONLY)" if self.args.read_only else "")

    def read_status(self) -> bool:
        """Leader of the fast poll group: three requests for every fast readback."""
        s = self.drv.get_basic()
        s["IsPpEnabled"] = self.drv.get_pulse_picker_enabled()
        ext = self.drv.get_group("ExternalControl")
        if "FastEnergyControlSource" not in ext:
            ext["FastEnergyControlSource"] = self.drv.get_fast_energy_control_source()
        if s.get("ActualShutterState") not in self.drv.SHUTTER_STATES:
            raise RuntimeError(f"unexpected shutter state {s.get('ActualShutterState')!r}")
        self._status, self._external = s, ext
        return s["ActualShutterState"] == "Opened"

    def slow(self) -> dict:
        """The slow group's values, fetched once per poll round."""
        if time.monotonic() - self._slow_t > 1.0:
            d = self.drv
            sc = d.get_group("StretcherCompressor")
            hp = d.get_optional("/Advanced/ActualHarmonicPower")
            self._slow = {
                "CompressorPosition": sc.get("ActualPosition"), "CompressorRange": _range(sc.get("PositionRange")),
                "DurationRange": f"{_range(sc.get('PulseDurationNegativeChirpRange'))} ; "
                                 f"{_range(sc.get('PulseDurationPositiveChirpRange'))}" if sc else "",
                "ActualRaFrequency": d.get_optional("/Advanced/ActualRaFrequency"),
                "HarmonicPower": None if hp is None or hp == -1 else hp,  # -1 = not measured
                "RemoteInterlock": d.get_optional("/Advanced/IsRemoteInterlockActive"),
                "ChillerTemp": d.get_optional("/Chiller/ActualTemperature"),
            }
            self._slow_t = time.monotonic()
        return self._slow

    def read_presets(self) -> str:
        presets = self.drv.get_optional("/Advanced/Presets") or []
        names = [p.get("Name") or p.get("Title") or str(p) if isinstance(p, dict) else str(p) for p in presets]
        return "; ".join(f"{i}: {n}" for i, n in enumerate(names))[:2047]

    def write_shutter(self, is_open: bool) -> None:
        log.warning("ShutterOpen <- %s", "OPENING" if is_open else "closing")
        self.drv.set_shutter_open(bool(is_open))

    def write_energy_control(self, source: str) -> None:
        log.warning("EnergyControl <- %s", source)
        self.drv.set_fast_energy_control_source(source)


if __name__ == "__main__":
    run_instrument(Carbide)
