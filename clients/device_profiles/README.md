# Dashboard device profiles (optional overrides)

Instruments (`ioc_host/common/devkit.py`) need nothing here: the dashboard builds their page from
the schema the IOC publishes (`fastq/profiles.py`): a field per readable param, a control per writable
param and action. A `<type>.json` here replaces that generated profile, to curate what a type shows.

```json
{
  "fields":   [{"suffix": "Temp", "label": "Temp (K)", "precision": 4}],
  "overview": ["Temp", "Setpoint_RBV"],
  "trend":    "Temp",
  "controls": [
    {"type": "setpoint", "label": "Setpoint (K)", "write_suffix": "Setpoint", "precision": 3},
    {"type": "enum", "label": "Mode", "write_suffix": "HeaterGasMode", "readback_suffix": "HeaterGasMode_RBV",
     "choices": ["MANUAL", "AM", "MA", "AUTO"]},
    {"type": "toggle", "label": "Heater to auto", "write_suffix": "AutoHeater"},
    {"type": "text", "label": "ROIs", "write_suffix": "Rois"},
    {"type": "action", "label": "Home", "write_suffix": "Home", "confirm": true, "doc": "moves to the limit"}
  ]
}
```

`fields` are shown (`"array": true` = plotted), `overview` on the control room / device list,
`trend` as the default trend. Only `controls` can be written from the dashboard - after a
confirmation, never while a plan runs, and not at all with `server.py --read-only`.
