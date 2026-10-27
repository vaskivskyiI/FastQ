# Adding an instrument

One file, on the PC the instrument is cabled to: `ioc_host/iocs/<type>_ioc.py`.
Nothing on the control host, in the web UI or in the AI tools has to change - they all
build themselves from the description the IOC publishes (`<prefix>IOC:SCHEMA`).

## 1. Write the class

```python
"""IOC: <maker model> (<interface>).

    python ioc_host/iocs/<type>_ioc.py --port COM5 --prefix FDM:GAUGE1:
    python ioc_host/iocs/<type>_ioc.py --mock --prefix TEST:GAUGE1:
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402


class Driver:            # talks to the hardware - blocking calls are fine
    ...


class SimDriver:         # same methods, no hardware (used with --mock and in tests)
    ...


class Gauge(Instrument):
    TYPE = "pfeiffer_tpg366"                 # = file name without _ioc
    DESCRIPTION = "Pfeiffer TPG366 vacuum gauge controller (6 channels)"
    DEFAULT_PREFIX = "FDM:GAUGE1:"
    POLL = 1.0                               # default read period (s)

    Pressure1 = Param(float, units="mbar", precision=3, get=lambda s: s.drv.pressure(1),
                      kind="hinted", overview=True, trend=True, doc="chamber 1")
    Setpoint = Param(float, units="K", limits=(1.5, 300), get="read_sp", set="write_sp",
                     positioner={"readback": "Temp", "tolerance": 0.05, "settle_s": 30, "timeout_s": 7200},
                     doc="what `mv(dev, 4.2)` / a scan axis sets")
    Mode = Param("enum", choices=("Off", "On"), get=..., set=..., kind="config")
    Degas = Action("degas", doc="start degassing", safety="confirm")

    @classmethod
    def add_arguments(cls, parser):
        parser.add_argument("--port", default="COM5")

    def open(self):                          # connect (called again after a lost connection)
        self.drv = SimDriver() if self.mock else Driver(self.args.port)

    def degas(self):
        self.drv.degas()


if __name__ == "__main__":
    run_instrument(Gauge)
```

### Param

| argument | meaning |
|---|---|
| dtype | `float`, `int`, `bool`, `str`, `"enum"` (+ `choices`), `"array"` (+ `nelm`) |
| get / set | method name or `lambda s, ...` - blocking driver code, runs in a worker thread |
| poll | seconds between reads; `None` = class `POLL`; `0` = only after connect and after puts |
| writable | implied by `set`; writable **without** `set` = a value the IOC keeps (`self.values[name]`) |
| limits | `(low, high)`, refused by the IOC, shown in the UI |
| kind | `hinted` (plotted), `normal` (in every reading), `config` (recorded once per run), `omitted` |
| positioner | makes the param a set-and-confirm axis: `readback` param, `tolerance`, `settle_s`, `timeout_s`, `moving` (a bool param) |
| overview / trend | dashboard overview card / trend line |
| refresh | other params to re-read right after a put of this one |
| event | an ELOG device event: `"change"` (a new non-empty value, e.g. an error list) or a value (`"Error"`, `"On"`) |
| safety | how dashboards write it: `"safe"` (at once: exposure, ranges, run parameters), `"confirm"` (default: asks first), `"protected"` (needs the lab PIN: beam, outputs, heater limits, interlocks). The server enforces it; lab.yaml `security.overrides` can change it per device |
| doc, units, precision, label | shown in the UI and to AI agents - write them |

PVs: read-only `Name`; writable with getter `Name` + `Name_RBV`; plus `LastError`,
`ErrorCount`, `Busy`, `Connected`, `IOC:SCHEMA`, `IOC:TYPE` and the iocStats `IOC:*`.

### Action
`Action(func, arg=None|float|int|str, doc=..., safety="safe"|"confirm"|"protected",
refresh=[params to re-read], timeout_s=..., exclusive=False, lock=True)`.
A put runs it; the put completes when the method returned (Bluesky waits on that).
Long measurements: `exclusive=True` refuses a second one at once (instead of queueing), the method calls
`self.publish(State=..., PointsDone=...)` to update read-only params meanwhile, and `lock=False` actions
(Abort, Stop) still reach it - they only set a `threading.Event`. `on_exit()` leaves the instrument safe
when the IOC stops.

`Instrument.ophyd` declares what the device is for the control host - never guessed from names:
`{"trigger": "Acquire" or {"action", "timeout_s", "require": {"param", "values", "message"}},
"stop": "<action dev.stop() runs>", "pulser": "<action that fires one shot>", "detector": "camera",
"positioner": "<param>"}`.

Several instruments of one type in one process (e.g. two cameras on one SDK):
`--also "--camera-index 2 --prefix FDM:CAMREF:"` (repeatable; the directory lists each prefix).

### Rules (learned the hard way, see HANDOFF.md)
- **Nothing is written to the instrument at startup or on connect.** Only puts write.
- All driver calls of one instrument are serialised (one lock) - don't add threads.
- Raise exceptions on failure; the IOC publishes them in `LastError`. `ConnectionError` /
  `OSError` / `TimeoutError` mark the instrument disconnected and `open()` is retried.
- A multi-axis controller is ONE instrument with one param per axis (each its own `positioner`).
- Readbacks are read-only params; never make a readback writable.
- Cameras: subclass `ioc_host.common.camera.Camera` and implement the driver interface
  documented there (`set_exposure/get_exposure/set_trigger/grab/abort`).

## 2. Try it without hardware

```bash
python ioc_host/iocs/<type>_ioc.py --print-schema
python ioc_host/iocs/<type>_ioc.py --mock --prefix TEST:X1:
caproto-get TEST:X1:Pressure1 TEST:X1:LastError
```
Add a line to `sim/sim_devices.yaml` to have it in the simulated lab.

## 3. Deploy

Add an entry to the IOC host's `ioc_host/supervisor_config.yaml` (this PC's own, not in git; or tick it in
the supervisor GUI), and to the matching `ioc_host/hosts/*.example.yaml` if the device belongs there for good:
```yaml
  gauge1:
    ioc_script: pfeiffer_tpg366_ioc
    enabled: true
    args: '--port COM5 --prefix FDM:GAUGE1:'
```
The directory announces it, `fastq.discovery` builds an ophyd device named `gauge1`,
the dashboards show it, and plans / the queue / AI agents can use it.

Name params like the other instruments of the class (control/fastq/roles.py): temperature controllers
`Temp` / `Setpoint` / `Heater` / `Ramping` / `RampTarget` / `RampTime` / `RampStart` / `RampStop`; lock-ins
`X` / `Y` / `R` / `Theta` / `Frequency` / `Amplitude` / `TimeConstant` / `Phase` / `AuxOut<n>` (front-panel
numbers); lasers `ShutterOpen` / `PPEnable` / `Attenuator` / `PPDivider` / `BurstCount` / `PulseDuration`;
motors `Position` / `Moving` / `Velocity` / `Acceleration` / `Home` / `Stop`. Then the plans work with it.
Simulators: a `SimDriver` in the IOC file, or for a big one `ioc_host/drivers/<name>_sim.py` (the IOC
host needs no `sim/` folder).

Optional extra: a dashboard profile `clients/device_profiles/<type>.json` overrides the generated one
(give each control a `safety`, else "confirm").

Instruments publish what they MEASURE. Quantities computed from that with experiment parameters (a
resistance from a lock-in voltage and a series resistor, a field from a current) are derived values of
the experiment (control/fastq/derived.py, dashboard Devices > derived) - not params of the IOC.

Long measurements (the Keithley IOC is the example): an `Action(..., exclusive=True)` holds the
instrument for the whole run (a second one is refused at once instead of queueing), calls
`self.publish(State=..., PointsDone=..., Readings=...)` to update read-only params meanwhile, and
`Action(..., lock=False)` commands (Abort, Stop) still get through to it - they only set a
`threading.Event` the run checks. `ophyd = {"trigger": {"action": "Acquire", "require": {"param":
"State", "values": ["Done"], "message": "Message"}}, "stop": "Abort"}` makes the device a detector
whose trigger fails unless the run ended Done, and whose stop() aborts. `on_exit()` runs when the IOC
stops (leave the instrument safe).
