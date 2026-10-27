# Experiment plans

Every `*.py` file here is loaded when the queue-server environment opens; each **public
generator function defined in the file** becomes a plan that can be queued (dashboard Queue page,
`clients/qs.py`, qserver). A file may instead list its plans explicitly in `PLANS = [...]`
(e.g. to offer plans imported from elsewhere, see `general.py`).

- **Add** a plan: write a function in an existing file, or copy `_template.py` to a new file.
- **Remove** a plan: delete the function / file, rename it with a leading `_`, or list it in
  `lab.yaml` -> `plans: {exclude: [name, ...]}`.
- **Apply**: dashboard Queue page -> "Reload plans" (or close + open the environment). Nothing
  else needs restarting. A file with an error is skipped (reported in the console), the rest loads.

Files starting with `_` are ignored. Building blocks: `fastq.plans` (scan = nd_scan, temperature,
lockin, laser, keithley, measurements) and `fastq.session.resolve(dev, "<role>")` (a device argument,
its name, "@role", or None = the role's device).

**Recipes** (a plan with fixed parameters - a temperature log, a thermal cycle, an I-V of a
project) are not plans: they are presets in `presets.yaml` (plan, kwargs, comment, setup), listed in the
dashboard's plan list under "recipes" and in each plan's preset menu. Add one there instead of writing
a wrapper function.

### Conventions (checked by tests/test_framework.py)
- Devices are parameters named after their lab role (`temperature=None`, `lockin=None`, `laser=None`,
  `keithley=None`, `probe=None`); None = the role's device. The dashboard offers a device list for
  every parameter named like a role, and for `detectors` / `readables` / `monitors`.
- Quantities carry their unit: `_s`, `_k`, `_a`, `_v`, `_fs`, `_ps`, `_deg`, `_steps`, `_percent`,
  `_hz`, `_ohm`, `_k_per_min`, `_v_per_s`; lists are plural (`temperatures_k`, `delays_ps`, `currents_a`).
- Temperature waits are `tolerance_k` + `settle_s`; counts are `repeats` (outer repetitions), `count`
  (readings per point) and `points`; aux outputs `aux_index` (front-panel numbers, 1..).
- Run metadata: `run_kind` (defaults to the plan name), `detectors` / `motors`, plan parameters, and
  `plot_hints` {x, signals, group, average, map_y} - the dashboard builds the plots from them
  (fastq.analysis.default_plots). Plans that emit light or current end in a safe state
  (`nd_scan(safe_state=...)` or a finalizer).

| file | plans |
|---|---|
| `general.py` | nd_scan, count, scan, set_temperature, rt_sweep, iv_curves, aux_waveform, laser_sequence_scan, wait_until, wait_for, log_until, sequence |
| `presets.yaml` | recipes: logs, R(T) cool-down / steps / thermal cycle, Keithley R(T), I-V, lock-in sweeps, flow |
| `transport.py` | TransportMeasure: R(T) steps / continuous, Delta, Pulse Delta, pulse sweep, single pulse |
| `fdm.py` | 4DMAG pump-probe: static, background, delay / waveplate / rotation / fluence scans, field loop, ROIs, live |
