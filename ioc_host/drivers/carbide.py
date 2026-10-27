"""Light Conversion CARBIDE / PHAROS REST client (HTTP JSON, http://<host>:<port>/<version>).

Endpoints as in the CARBIDE REST API documentation (v1: /Basic, /StretcherCompressor,
/ExternalControl, /Advanced, ...)
and Light Conversion's own client (pip package lightcon, laser_clients). PHAROS serves
the same endpoint families (4dmag used api v0 on port 20022).

Only put()/post() and the explicit set_* methods write; with read_only=True every
non-GET request is refused before it is sent. Network failures are retried and
then raised as ConnectionError (the IOC reconnects); HTTP errors (the laser
rejecting a command, 403 = "state forbids it / out of range") raise LaserHTTPError
at once. Endpoints a laser does not have (optional features: harmonics power meter,
chiller, SCIM ...) answer 404: get_optional() returns None for them and stops asking.

SimLaser is an in-memory model of the same endpoints, used by --mock and by
sim/mock_carbide_server.py.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Any

import requests


class LaserHTTPError(RuntimeError):
    def __init__(self, method: str, endpoint: str, status: int, text: str) -> None:
        super().__init__(f"{method} {endpoint}: HTTP {status} {text[:200]}")
        self.status = status


class CarbideLaser:
    SHUTTER_STATES = {"Opened", "Closed"}
    ENERGY_SOURCES = {"Internal", "External"}

    def __init__(self, host: str, port: int = 20010, version: str = "v1", timeout_s: float = 5.0,
                 retries: int = 3, read_only: bool = False) -> None:
        self.base_url = f"http://{host}:{port}/{version}"
        self.timeout_s = timeout_s
        self.retries = retries
        self.read_only = read_only
        self._session: requests.Session | None = None
        self._missing: set[str] = set()  # endpoints that answered 404

    def connect(self, probe: str = "/Basic/ActualStateName") -> None:
        self._session = requests.Session()
        self.get(probe)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def _request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        if self.read_only and method != "GET":
            raise PermissionError(f"read-only: refused {method} {endpoint}")
        if self._session is None:
            raise ConnectionError("not connected")
        last: Exception | None = None
        for _ in range(self.retries):
            try:
                r = self._session.request(method, self.base_url + endpoint, timeout=self.timeout_s, **kwargs)
            except requests.RequestException as exc:
                last = exc
                time.sleep(0.5)
                continue
            if not r.ok:
                raise LaserHTTPError(method, endpoint, r.status_code, r.text)
            return r
        raise ConnectionError(f"{method} {endpoint} failed after {self.retries} attempts: {last}")

    def get(self, endpoint: str) -> Any:
        return self._request("GET", endpoint).json()

    def get_optional(self, endpoint: str) -> Any:
        """None for an endpoint this laser does not have (404, remembered)."""
        if endpoint in self._missing:
            return None
        try:
            return self.get(endpoint)
        except LaserHTTPError as exc:
            if exc.status != 404:
                raise
            self._missing.add(endpoint)
            return None

    def put(self, endpoint: str, value: Any) -> None:
        self._request("PUT", endpoint, json=value)

    def post(self, endpoint: str) -> None:
        self._request("POST", endpoint)

    _put, _post = put, post  # old names

    # ------------------------------------------------------------ reads
    def get_basic(self) -> dict:
        data = self.get("/Basic")
        if not isinstance(data, dict):
            raise RuntimeError(f"unexpected /Basic reply: {data!r}")
        return data

    def get_group(self, group: str) -> dict:
        """GET /<group> (all GETable properties of /StretcherCompressor, /ExternalControl ...); {} if absent."""
        data = self.get_optional("/" + group)
        return data if isinstance(data, dict) else {}

    def get_pulse_picker_enabled(self) -> bool:
        return bool(self.get("/Advanced/IsPpEnabled"))

    def get_fast_energy_control_source(self) -> str:
        return str(self.get("/ExternalControl/FastEnergyControlSource"))

    # ------------------------------------------------------------ writes
    def set_shutter_open(self, is_open: bool) -> None:
        self.post("/Basic/EnableOutput" if is_open else "/Basic/CloseOutput")

    def set_attenuator(self, percent: float) -> None:
        if not 0.0 <= percent <= 100.0:
            raise ValueError("attenuator must be 0..100 %")
        self.put("/Basic/TargetAttenuatorPercentage", percent)

    def set_pulse_duration(self, fs: float) -> None:
        if fs == 0 or fs != fs:  # sign is chirp polarity; the laser checks the range
            raise ValueError("pulse duration must be a non-zero number")
        self.put("/Basic/TargetPulseDuration", fs)

    def set_pulse_picker_divider(self, divider: int) -> None:
        if divider < 1:
            raise ValueError("divider must be >= 1")
        self.put("/Basic/TargetPpDivider", divider)

    def set_burst_pulse_count(self, count: int) -> None:
        if count < 0:
            raise ValueError("burst count must be >= 0")
        self.put("/Basic/TargetBurstPulseCount", count)

    def set_pulse_picker_enabled(self, enabled: bool) -> None:
        self.post("/Advanced/EnablePp" if enabled else "/Advanced/DisablePp")

    def set_fast_energy_control_source(self, source: str) -> None:
        if source not in self.ENERGY_SOURCES:
            raise ValueError(f"source must be one of {sorted(self.ENERGY_SOURCES)}")
        self.put("/ExternalControl/FastEnergyControlSource", source)


def issues_text(items) -> str:
    """/Basic Errors / Warnings: [{"Code": "W0600", "Description": "..."}] -> 'W0600 ...; ...'."""
    out = []
    for it in items or []:
        if isinstance(it, dict):
            out.append(f"{it.get('Code', '')} {it.get('Description', '')}".strip())
        else:
            out.append(str(it))
    return "; ".join(out)


# ---------------------------------------------------------------------- simulator
STATE_IDS = {"StandingBy": 1, "Service": 2, "InFieldUpdate": 4, "Initializing": 8, "GoingToStandby": 16,
             "Failure": 32, "Housekeeping": 64, "Operational": 128}
TRIGGER_SOURCES = ("Internal", "ExternalLow", "ExternalHigh")
SYNC_CONFIGS = ("Disabled", "PpOpensAndClosesAutomatically", "PpClosesOnFrequencyError")


class SimLaser:
    """The documented endpoints of a CARBIDE (flavour "carbide") or PHAROS ("pharos", the
    4dmag v0 endpoints on top) in memory. get/put/post(endpoint) like CarbideLaser;
    errors raise LaserHTTPError with the status the real laser documents (403, 404).
    Moving parts (attenuator, compressor, RA frequency) approach their target within seconds."""

    def __init__(self, flavour: str = "carbide", read_only: bool = False) -> None:
        self.flavour, self.read_only = flavour, read_only
        self.lock = threading.RLock()
        self.f_ra = 60.0 if flavour == "carbide" else 200.0  # kHz
        self.state, self.t_state = "Operational", 0.0
        self.basic = {
            "Warnings": [], "Errors": [], "ActualPpDivider": 200, "TargetPpDivider": 200,
            "TargetPulseDuration": -220.8, "ActualPulseDuration": -220.8, "ActualHarmonic": 1,
            "IsOutputEnabled": False, "IsEmissionWarningActive": False, "TargetAttenuatorPercentage": 10.196078,
            "ActualAttenuatorPercentage": 10.196078, "ActualShutterState": "Closed", "SelectedPresetIndex": 0,
            "LastExecutedPresetIndex": 0, "GeneralStatus": "", "ActualBurstPulseCount": 1000,
            "TargetBurstPulseCount": 1000, "IsPowerlockEnabled": True, "SerialNumber": "SIM00001",
            "LaserIdentificationNumber": "D00001", "FirmwareVersion": "sim", "TargetRaEnergy": 40.0,
        }
        self.compressor = {"PositionRange": {"From": -15.64, "To": 1.29},
                           "PulseDurationPositiveChirpRange": {"From": 221, "To": 1500},
                           "PulseDurationNegativeChirpRange": {"From": -221, "To": -15000},
                           "TargetPosition": 0.0, "ActualPosition": 0.0}
        self.external = {"ExternalSyncConfiguration": "Disabled", "AomTriggerSource": "Internal",
                         "AnalogControlSource": "Internal", "FastEnergyControlSource": "Internal",
                         "HvFastEnergyControlSource": "Internal", "PpTriggerSource": "Internal"}
        self.advanced = {"IsPpEnabled": False, "TargetRaFrequency": self.f_ra, "ActualRaFrequency": self.f_ra,
                         "IsRemoteInterlockActive": False, "ActualHarmonicPower": -1,
                         "AvailableAttenuatorPercentageRange": {"From": 0.0, "To": 100.0},
                         "TargetPulseCount": 0, "ActualPulseCount": 0,
                         "Presets": [{"Name": "60 kHz 1030 nm"}, {"Name": "6 kHz 1030 nm"}, {"Name": "60 kHz 515 nm"}]}
        self.preset_harmonic = [1, 1, 2]
        self.writes: list[str] = []
        self._t = time.monotonic()
        self._pp_off_at = 0.0

    # -- dynamics
    def _update(self) -> None:
        now = time.monotonic()
        dt, self._t = now - self._t, now
        b, a, c = self.basic, self.advanced, self.compressor
        if self.t_state and now > self.t_state:  # Housekeeping / GoingToStandby end
            self.state = "Operational" if self.state == "Housekeeping" else "StandingBy"
            self.t_state = 0.0
        step = 20.0 * dt  # % per s
        d = b["TargetAttenuatorPercentage"] - b["ActualAttenuatorPercentage"]
        b["ActualAttenuatorPercentage"] += max(-step, min(step, d))
        b["ActualPpDivider"] = b["TargetPpDivider"]
        b["ActualBurstPulseCount"] = b["TargetBurstPulseCount"]
        d = b["TargetPulseDuration"] - b["ActualPulseDuration"]
        b["ActualPulseDuration"] += max(-500 * dt, min(500 * dt, d))
        c["ActualPulseDuration"], c["TargetPulseDuration"] = b["ActualPulseDuration"], b["TargetPulseDuration"]
        d = c["TargetPosition"] - c["ActualPosition"]
        c["ActualPosition"] += max(-2 * dt, min(2 * dt, d))
        d = a["TargetRaFrequency"] - a["ActualRaFrequency"]
        a["ActualRaFrequency"] += max(-dt, min(dt, d))
        a["ActualPulseCount"] = a["TargetPulseCount"]
        if self._pp_off_at and now > self._pp_off_at:  # a finite burst switches the picker off when done
            a["IsPpEnabled"], self._pp_off_at = False, 0.0
        running = self.state == "Operational"
        b["ActualStateName"] = self.state
        b["GeneralStatus"] = "" if running else self.state
        b["IsEmissionWarningActive"] = running and b["IsOutputEnabled"]
        b["ActualShutterState"] = "Opened" if b["IsOutputEnabled"] else "Closed"
        f_out = a["ActualRaFrequency"] / max(b["ActualPpDivider"], 1)
        b["ActualOutputFrequency"] = f_out if running else 0.0
        on = running and b["IsOutputEnabled"] and a["IsPpEnabled"]
        e = b["TargetRaEnergy"] * b["ActualAttenuatorPercentage"] / 100 * (1 + 0.003 * random.gauss(0, 1))
        e /= b["ActualHarmonic"] * 1.5 if b["ActualHarmonic"] > 1 else 1
        b["ActualOutputEnergy"] = e if running else 0.0
        b["ActualOutputPower"] = e * f_out * 1e-3 if on else 0.0
        a["ActualHarmonicPower"] = b["ActualOutputPower"] if b["ActualHarmonic"] > 1 else -1

    def _fail(self, method, endpoint, status, text):
        raise LaserHTTPError(method, endpoint, status, text)

    def _table(self, group: str) -> dict | None:
        if group == "Basic":
            return {**self.basic, "ActualStateName2": self.state} if self.flavour == "pharos" else self.basic
        return {"StretcherCompressor": self.compressor, "ExternalControl": self.external}.get(group)

    # -- REST surface
    def get(self, endpoint: str) -> Any:
        with self.lock:
            self._update()
            group, _, key = endpoint.strip("/").partition("/")
            if group == "Advanced":
                if key == "ActualStateId":
                    return STATE_IDS[self.state]
                if key in self.advanced:
                    return self.advanced[key]
                self._fail("GET", endpoint, 404, "not found")
            table = self._table(group)
            if table is None:
                self._fail("GET", endpoint, 404, "not found")
            if not key:
                return dict(table)
            if key not in table:
                self._fail("GET", endpoint, 404, "not found")
            return table[key]

    def get_optional(self, endpoint: str) -> Any:
        try:
            return self.get(endpoint)
        except LaserHTTPError as exc:
            if exc.status == 404:
                return None
            raise

    def _check(self, method: str, endpoint: str) -> None:
        if self.read_only:
            raise PermissionError(f"read-only: refused {method} {endpoint}")
        self.writes.append(f"{method} {endpoint}")

    def put(self, endpoint: str, value: Any) -> None:
        with self.lock:
            self._check("PUT", endpoint)
            self._update()
            group, _, key = endpoint.strip("/").partition("/")
            b, a, c, x = self.basic, self.advanced, self.compressor, self.external
            busy = self.state != "Operational"
            if group == "Basic" and key == "TargetAttenuatorPercentage":
                if not 0 <= float(value) <= 100 or busy:
                    self._fail("PUT", endpoint, 403, "out of bounds or state forbids it")
                b[key] = float(value)
            elif group == "Basic" and key == "TargetPpDivider":
                if int(value) < 1 or busy:
                    self._fail("PUT", endpoint, 403, "out of bounds or state forbids it")
                b[key] = int(value)
            elif group == "Basic" and key == "TargetBurstPulseCount":
                b[key] = int(value)
            elif group in ("Basic", "StretcherCompressor") and key == "TargetPulseDuration":
                v = float(value)
                pos, neg = c["PulseDurationPositiveChirpRange"], c["PulseDurationNegativeChirpRange"]
                if not (pos["From"] <= v <= pos["To"] or neg["To"] <= v <= neg["From"]):
                    self._fail("PUT", endpoint, 403, "pulse duration is out of range")
                b[key] = v
            elif group == "Basic" and key == "SelectedPresetIndex":
                if not 0 <= int(value) < len(a["Presets"]):
                    self._fail("PUT", endpoint, 403, "incorrect preset index")
                b[key] = int(value)
            elif group == "StretcherCompressor" and key == "TargetPosition":
                r = c["PositionRange"]
                if not r["From"] <= float(value) <= r["To"]:
                    self._fail("PUT", endpoint, 403, "target position is out of range")
                c[key] = float(value)
            elif group == "ExternalControl" and key in x:
                allowed = {"AomTriggerSource": TRIGGER_SOURCES, "ExternalSyncConfiguration": SYNC_CONFIGS,
                           "PpTriggerSource": ("Internal", "ExternalActiveLow", "ExternalActiveHigh")}
                if value not in allowed.get(key, ("Internal", "External")):
                    self._fail("PUT", endpoint, 403, "configuration is not supported")
                x[key] = value
            elif group == "Advanced" and key in ("TargetRaFrequency", "TargetPulseCount"):
                a[key] = type(a[key])(value)
            else:
                self._fail("PUT", endpoint, 404, "not found")

    def post(self, endpoint: str) -> None:
        with self.lock:
            self._check("POST", endpoint)
            self._update()
            group, _, key = endpoint.strip("/").partition("/")
            b, a = self.basic, self.advanced
            if key == "EnableOutput":
                if self.state not in ("Operational", "Service"):
                    self._fail("POST", endpoint, 403, "laser is not in operational or service state")
                b["IsOutputEnabled"] = True
            elif key == "CloseOutput":
                b["IsOutputEnabled"] = False
            elif key in ("EnablePp", "DisablePp"):
                a["IsPpEnabled"] = key == "EnablePp"
                n = a["TargetPulseCount"] if self.flavour == "pharos" else b["TargetBurstPulseCount"]
                div = max(b["TargetPpDivider"], 1)
                # like the real laser: a finite burst disables the picker once delivered
                self._pp_off_at = (time.monotonic() + min(n * div / (a["ActualRaFrequency"] * 1e3), 5.0)
                                   if key == "EnablePp" and n >= 1 else 0.0)
            elif key == "ApplySelectedPreset":
                if self.state not in ("Operational", "StandingBy"):
                    self._fail("POST", endpoint, 403, "state forbids executing preset")
                i = b["SelectedPresetIndex"]
                b["LastExecutedPresetIndex"], b["ActualHarmonic"] = i, self.preset_harmonic[i]
                b["Errors"], b["IsOutputEnabled"] = [], False
                self.state, self.t_state = "Housekeeping", time.monotonic() + 3.0
            elif key == "GoToStandby":
                if self.state != "Operational":
                    self._fail("POST", endpoint, 403, "state forbids standby")
                b["IsOutputEnabled"], a["IsPpEnabled"] = False, False
                self.state, self.t_state = "GoingToStandby", time.monotonic() + 2.0
            elif key in ("EnablePowerlock", "DisablePowerlock"):
                b["IsPowerlockEnabled"] = key == "EnablePowerlock"
            elif key == "ResetRemoteInterlock":
                a["IsRemoteInterlockActive"] = False
            elif key in ("ReduceLeak", "SaveAsShortestImpulseDuration"):
                pass
            else:
                self._fail("POST", endpoint, 404, "not found")

    # the CarbideLaser convenience methods, on the same surface
    get_basic = lambda self: self.get("/Basic")  # noqa: E731
    get_group = lambda self, group: self.get_optional("/" + group) or {}  # noqa: E731
    get_pulse_picker_enabled = lambda self: bool(self.get("/Advanced/IsPpEnabled"))  # noqa: E731
    get_fast_energy_control_source = lambda self: self.get("/ExternalControl/FastEnergyControlSource")  # noqa: E731
    set_shutter_open = CarbideLaser.set_shutter_open
    set_attenuator = CarbideLaser.set_attenuator
    set_pulse_duration = CarbideLaser.set_pulse_duration
    set_pulse_picker_divider = CarbideLaser.set_pulse_picker_divider
    set_burst_pulse_count = CarbideLaser.set_burst_pulse_count
    set_pulse_picker_enabled = CarbideLaser.set_pulse_picker_enabled
    set_fast_energy_control_source = CarbideLaser.set_fast_energy_control_source
    SHUTTER_STATES, ENERGY_SOURCES = CarbideLaser.SHUTTER_STATES, CarbideLaser.ENERGY_SOURCES

    def connect(self, probe: str = "") -> None:
        pass

    def close(self) -> None:
        pass
