"""IOC: Light Conversion PHAROS laser (REST API, http://<host>:20022/v0/).

    python ioc_host/iocs/pharos_ioc.py --host 192.168.2.10 --prefix FDM:PHAROS:
    python ioc_host/iocs/pharos_ioc.py --host 192.168.2.10 --read-only --prefix FDM:PHAROS:   (monitoring only)
    python ioc_host/iocs/pharos_ioc.py --mock --prefix TEST:PHAROS:

PHAROS speaks the same Light Conversion REST API as the CARBIDE (4dmag: api v0 on
port 20022; Light Conversion's own client: v1 on 20020 - use --port/--api), so the
HTTP client is ioc_host/drivers/carbide.py. The core endpoints are the ones 4dmag
fdm/lighcon.py used, one GET per value. The rest (harmonic, pulse duration,
presets, standby, powerlock, errors / warnings, RA frequency, remote interlock) are
the endpoints of the CARBIDE REST API documentation - not checked on this PHAROS:
one it does not serve answers 404 and its PV stays blank (actions report the 404 in
LastError). Harmonic / output selection over REST is done by presets.

SAFETY: nothing is sent to the laser at startup or by polling (GETs only).
Opening the output, the pulse picker, attenuator, divider, trigger source and
presets are "protected"; closing (CloseOutput, DisablePP) is "safe" - one click.
--read-only refuses every non-GET request in the driver.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402
from ioc_host.drivers.carbide import CarbideLaser, SimLaser, issues_text  # noqa: E402

PP_TRIGGER = ("Internal", "ExternalActiveLow", "ExternalActiveHigh")
SLOW = 5.0


class Driver(CarbideLaser):
    """CARBIDE client pointed at the PHAROS API (connect probes ActualStateName2, which lighcon.py used)."""

    def __init__(self, host: str, port: int = 20022, version: str = "v0", read_only: bool = False) -> None:
        super().__init__(host, port=port, version=version, read_only=read_only)

    def connect(self, probe: str = "/Basic/ActualStateName2") -> None:
        super().connect(probe)


def _get(endpoint: str):
    return lambda s: s.drv.get(endpoint)


def _opt(endpoint: str, cast=None):
    """Optional endpoint (CARBIDE documentation, unverified on PHAROS): None (blank) if absent."""
    def get(s):
        v = s.drv.get_optional(endpoint)
        return v if v is None or cast is None else cast(v)
    return get


class Pharos(Instrument):
    TYPE = "pharos"
    DESCRIPTION = "Light Conversion PHAROS femtosecond laser (REST API v0)"
    DEFAULT_PREFIX = "FDM:PHAROS:"
    POLL = 1.0

    # same names as the CARBIDE (fastq/roles.py "laser"): ShutterOpen / PPEnable settable, closing one-click safe
    ShutterOpen = Param(bool, get=_get("/Basic/IsOutputEnabled"), set=lambda s, v: s.drv.set_shutter_open(bool(v)),
                        poll=0.5, overview=True, safety="protected",
                        refresh=["OutputPower", "OutputEnergy"], doc="laser output (shutter) open")
    PPEnable = Param(bool, get=_get("/Advanced/IsPpEnabled"), set=lambda s, v: s.drv.set_pulse_picker_enabled(bool(v)),
                     poll=0.5, overview=True, safety="protected", refresh=["OutputFrequency", "OutputPower"],
                     doc="pulse picker on")
    State = Param(str, get=_get("/Basic/ActualStateName2"), poll=2.0, max_length=256, overview=True,
                  doc="laser state")
    GeneralStatus = Param(str, get=_get("/Basic/GeneralStatus"), poll=2.0, max_length=1024,
                   doc="general status message")
    Errors = Param(str, event="change", get=_opt("/Basic/Errors", issues_text), poll=2.0, kind="config",
                   max_length=1024,
                   doc="active errors (latched until laser start / preset execution)")
    Warnings = Param(str, get=_opt("/Basic/Warnings", issues_text), poll=2.0, kind="config", max_length=1024,
                     doc="active warnings")
    EmissionWarning = Param(bool, get=_opt("/Basic/IsEmissionWarningActive"), poll=2.0, doc="emission warning active")
    OutputFrequency = Param(float, units="kHz", precision=3, get=_get("/Basic/ActualOutputFrequency"),
                            overview=True)
    OutputPower = Param(float, units="W", precision=3, get=_get("/Basic/ActualOutputPower"), kind="hinted",
                        overview=True, trend=True)
    OutputEnergy = Param(float, units="uJ", precision=2, get=_get("/Basic/ActualOutputEnergy"), overview=True)
    Harmonic = Param(int, get=_opt("/Basic/ActualHarmonic", int), poll=SLOW, kind="config",
                     doc="harmonic at the output (1 = fundamental); selected by the presets")
    Attenuator = Param(float, units="%", precision=2, limits=(0.0, 100.0), safety="protected",
                       get=_get("/Basic/ActualAttenuatorPercentage"), set=lambda s, v: s.drv.set_attenuator(v),
                       positioner={"tolerance": 0.5, "settle_s": 0.0, "timeout_s": 60.0},
                       overview=True, doc="output attenuator (power)")
    PPDivider = Param(int, limits=(1, 1_000_000), get=_get("/Basic/ActualPpDivider"), safety="protected",
                      set=lambda s, v: s.drv.set_pulse_picker_divider(v), poll=2.0, kind="config",
                      doc="pulse-picker divider: output rate = RA rate / divider (lower = more power)")
    BurstCount = Param(int, limits=(0, 100_000_000), get=_get("/Advanced/ActualPulseCount"),
                       set=lambda s, v: s.drv.put("/Advanced/TargetPulseCount", v), poll=2.0, kind="config",
                       doc="pulses per burst, 0 = continuous")
    PulseDuration = Param(float, units="fs", precision=1, get=_opt("/Basic/ActualPulseDuration"), poll=2.0,
                          set=lambda s, v: s.drv.set_pulse_duration(v), kind="config",
                          positioner={"tolerance": 5.0, "settle_s": 0.0, "timeout_s": 60.0},
                          doc="pulse duration (sign = chirp polarity); moves the compressor")
    CompressorPosition = Param(float, precision=3, get=_get("/StretcherCompressor/ActualPosition"),
                               set=lambda s, v: s.drv.put("/StretcherCompressor/TargetPosition", v), poll=2.0,
                               kind="config", doc="compressor position (pulse duration / chirp), laser units")
    TriggerSource = Param("enum", choices=PP_TRIGGER, get=_get("/ExternalControl/PpTriggerSource"),
                            set=lambda s, v: s.drv.put("/ExternalControl/PpTriggerSource", v), poll=5.0,
                            kind="config", safety="protected",
                            doc="pulse-picker trigger (external: pulses whenever the TTL input says so)")
    Powerlock = Param(bool, get=_opt("/Basic/IsPowerlockEnabled"), poll=SLOW, kind="config",
                      set=lambda s, v: s.drv.post("/Basic/EnablePowerlock" if v else "/Basic/DisablePowerlock"),
                      doc="RA powerlock (output power stabilisation)")
    PresetIndex = Param(int, limits=(0, 99), get=_opt("/Basic/SelectedPresetIndex", int), poll=SLOW, kind="config",
                        set=lambda s, v: s.drv.put("/Basic/SelectedPresetIndex", int(v)),
                        doc="selected preset (0-based; ApplyPreset executes it)")
    LastPreset = Param(int, get=_opt("/Basic/LastExecutedPresetIndex", int), poll=SLOW, kind="config",
                       doc="last executed preset")
    Presets = Param(str, poll=0, kind="config", max_length=2048, get="read_presets", doc="presets: index: name")
    RaFrequency = Param(float, units="kHz", precision=3, get=_opt("/Advanced/ActualRaFrequency"), poll=SLOW,
                        kind="config", doc="RA repetition rate before the pulse picker")
    RemoteInterlock = Param(bool, get=_opt("/Advanced/IsRemoteInterlockActive"), poll=SLOW, overview=True,
                            doc="remote interlock active")
    SerialNumber = Param(str, poll=0, kind="config", get=_opt("/Basic/SerialNumber", str))
    FirmwareVersion = Param(str, poll=0, kind="config", get=_opt("/Basic/FirmwareVersion", str))

    CloseOutput = Action(lambda s: s.drv.set_shutter_open(False), safety="safe", doc="close the laser output",
                         refresh=["ShutterOpen", "OutputPower", "OutputEnergy"])
    DisablePP = Action(lambda s: s.drv.set_pulse_picker_enabled(False), safety="safe",
                       doc="disable the pulse picker", refresh=["PPEnable", "OutputFrequency", "OutputPower"])
    ApplyPreset = Action(lambda s: s.drv.post("/Basic/ApplySelectedPreset"), safety="protected", timeout_s=30.0,
                         doc="execute the selected preset (may change harmonic, rate and power; also brings the "
                             "laser out of standby)")
    Standby = Action(lambda s: s.drv.post("/Basic/GoToStandby"),
                     doc="send the laser to standby (stops emission; back with ApplyPreset)")
    ResetRemoteInterlock = Action(lambda s: s.drv.post("/Advanced/ResetRemoteInterlock"), safety="protected",
                                  doc="reset the remote interlock (re-arms the laser)")

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--host", help="laser IP address (required unless --mock)")
        parser.add_argument("--port", type=int, default=20022)
        parser.add_argument("--api", default="v0", help="API version in the URL (4dmag: v0)")
        parser.add_argument("--read-only", action="store_true", help="refuse every command to the laser")

    def open(self) -> None:
        if self.mock:
            self.drv = SimLaser("pharos", read_only=self.args.read_only)
            return
        if not self.args.host:
            raise SystemExit("--host is required")
        self.drv = Driver(self.args.host, self.args.port, self.args.api, self.args.read_only)
        self.drv.connect()

    def read_presets(self) -> str:
        presets = self.drv.get_optional("/Advanced/Presets") or []
        names = [p.get("Name") or p.get("Title") or str(p) if isinstance(p, dict) else str(p) for p in presets]
        return "; ".join(f"{i}: {n}" for i, n in enumerate(names))[:2047]


if __name__ == "__main__":
    run_instrument(Pharos)
