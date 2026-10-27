# FastQ experiment control (EPICS + Bluesky)

Decentralised, universal measurement system for lab setups: transport (temperature,
laser, current/voltage, lock-in, later field) and 4DMAG (HHG pump-probe: spectrometer cameras,
delay line, waveplates, shutters, gas flow, magnet). Instruments are served as EPICS Channel Access
PVs by IOCs on whichever PC they are wired to; experiments are Bluesky plans run by a queue server;
data goes to a Tiled data server (big detector frames stay in HDF5 files on the detector PC); every
run is logged to ELOG; AI assistants get the lab through an MCP server. Every layer is standard
EPICS / Bluesky practice.

**Extending it**
- a new instrument = one file `ioc_host/iocs/<type>_ioc.py` describing its parameters
  ([docs/ADDING_A_DEVICE.md](docs/ADDING_A_DEVICE.md)); the IOC publishes that description
  (`<prefix>IOC:SCHEMA`) and the control host, dashboards and AI tools build themselves from it;
- a new kind of scan = usually one call of `nd_scan` (any nesting of any settable axes, set-and-confirm,
  repeat-until-stopped, synchronised hardware-triggered cameras) in a file in `control/experiments/`;
- a new setup = a `control/lab.<setup>.example.yaml` (roles, ELOG notebooks) + `ioc_host/hosts/<pc>.example.yaml`
  per IOC PC.

**Config files per PC.** Each PC runs from its own `control/lab.yaml` (control host) and
`ioc_host/supervisor_config.yaml` (IOC host). They are not in git, so `git pull` never overwrites the real
addresses; `start_control_host.bat` / `start_ioc_host.bat` create them on the first start from one of the
tracked examples below (edit, start again). The repository is public: examples carry placeholder
addresses. Real configs to reuse on other lab PCs go in a private folder with the same layout
(`control/`, `ioc_host/hosts/`), e.g. `../FastQ-private` next to the repository or `FASTQ_CONFIG_EXAMPLES=<folder>`
(a NAS share, a private git repository); the start scripts offer its examples too.

| setup | control config | IOC hosts | recipes |
|---|---|---|---|
| transport (R(T), I-V, laser) | `control/lab.transport.example.yaml` | `ioc_host/hosts/transport.example.yaml` | `transport.py`, `general.py`, recipes in `presets.yaml` |
| 4DMAG (HHG pump-probe) | `control/lab.4dmag.example.yaml` | `ioc_host/hosts/4dmag.example.yaml` | `experiments/fdm.py` |
| simulator | `control/lab_sim.yaml` | `sim/sim_devices.yaml` (+ `sim/sim_host2.yaml`) | all |

```
          ┌────────────────────────── lab LAN 192.168.1.x ───────────────────────────┐
          │                                                                           │
 GPIB ─── │ IOC HOST (transport PC)                          MFLI .94     Carbide .10   │
 ITC503,  │   ioc_host/  supervisor -> one IOC per device ──LabOne────────REST        │
 Keithley │              directory IOC  FASTQ:DIR:                                    │
          │                 │  Channel Access (UDP 5064 name search + TCP)            │
          │ CONTROL HOST (normally the same PC)                                       │
          │   control/   Redis + Tiled :8000 + queue server :60615 + dashboards :8088 │
          │              ELOG service ──────────────────────────────► ELOG server     │
          └──────────────────────────────┬────────────────────────────────────────────┘
                                         │ TCP only (LAN, or SSH tunnel via the router)
                  CLIENTS (any PC): web browser -> http://<control host>:8088
                                    clients/  qs.py, export_run.py
```

| folder | runs on | what |
|---|---|---|
| `ioc_host/` | IOC host (PC with the instrument cables) | `iocs/*_ioc.py` one IOC per instrument type, `drivers/`, `supervisor/` (starts/restarts IOCs), `supervisor_config.yaml` (which IOCs, addresses: this PC's own, not in git), `hosts/*.example.yaml` |
| `control/` | control host (default: the IOC host) | `experiments/` the plans (one file per topic, auto-loaded); `fastq/` Bluesky package: `generic.py` (ophyd devices from IOC schemas), `discovery.py`, `plans/` building blocks, `elog.py`; `web/` dashboards; `lab.yaml`; `start_control_host.py` |
| `clients/` | any PC | `qs.py` queue client, `export_run.py` CSV / HDF5 export (`fastq.analysis`) |
| `sim/` | any PC (testing) | the same IOCs with simulated instruments (`TEST:` prefixes) |
| `tests/` | IOC host / any | hardware tests (`hw_*.py`), simulator plan run (`run_sim_plans.py`), framework tests (`test_framework.py`) |
| `docs/` | - | `ADDING_A_DEVICE.md` |

## EPICS conventions used everywhere

- PV names `<prefix><Name>`, prefix per instrument instance: `FASTQ:ITC2:Temp`, `FASTQ:MFLI1:Resistance`.
- Setpoint `X` + readback `X_RBV` (areaDetector style; ophyd `EpicsSignalWithRBV`). Plain read-only values have no suffix.
- Units (EGU) and precision on every analog PV; standard `.SCAN` field sets poll rates (`caput FASTQ:MFLI1:X.SCAN ".1 second"`).
- iocStats on every IOC: `<prefix>IOC:HEARTBEAT`, `UPTIME`, `STARTTOD`, `HOSTNAME`, `SYSRESET` (= restart; the supervisor brings it back, like procServ) - plus `<prefix>IOC:TYPE`.
- Access security: `ioc_host/security.yaml` (see `.example`) limits which hosts/users may write.
- Nothing is written to an instrument when an IOC starts; only explicit puts to setpoint PVs act.

## Discovery (no IP addresses or prefixes in client code)

1. **PVs by name**: Channel Access finds any PV by UDP broadcast name search - clients need only the name.
2. **Devices**: the directory IOC publishes `FASTQ:DIR:Devices` (JSON: name, type, prefix) from `supervisor_config.yaml`.
   `fastq.discovery.discover()` reads it, checks each `<prefix>IOC:TYPE`, and builds the ophyd devices.
3. **Roles**: `control/lab.yaml` only says which device plays which part (`temperature: itc2`, ...).

```bash
cd control && python -m fastq.discovery          # what exists and what is online
python clients/export_run.py --list               # recent runs; export_run.py 905 [--hdf5] exports one
```

## Starting everything

On Windows, double-click (or run from a console) one file in the repository folder. Each one creates its
own virtual environment the first time (a Python 3.10-3.13: `py` launcher, else `python` on PATH, or set
`FASTQ_PYTHON`), installs its requirements, and later re-installs only when a requirements file changed;
a venv that no longer starts (copied from another PC) is moved aside and made again (`tools\ensure_venv.bat`).

| file | part | venv |
|---|---|---|
| `start_ioc_host.bat [host file] [--headless]` | IOC host: supervisor GUI (or no window) for this PC's IOCs | `.venv` |
| `start_control_host.bat [lab file]` | control host: Tiled, queue server, dashboards :8088, ELOG, derived values, backups, AI access | `.venv-control` |
| `start_simulator.bat` | the whole lab without hardware: mock ELOG + simulated IOC host + control host (lab_sim.yaml), each in a window | `.venv-dev` |
| `start_notebooks.bat [lab file]` | JupyterLab with the analysis notebooks | `.venv-analysis` |
| `run_tests.bat [--sim]` | unit tests (+ the measurement plans against a running simulator) | `.venv-dev` |

To start the IOC host with Windows instead: `deploy\install_ioc_host.ps1` ([deploy/README.md](deploy/README.md)).
Linux / by hand: `pip install -r requirements-ioc-host.txt` (+ `requirements-control.txt`), then
`python ioc_host/supervisor/supervisor.py --config <host file>` and `python control/start_control_host.py --lab <lab file>`.
Then open **http://localhost:8088** (on the control host) or http://<control host>:8088 from the LAN.
**Clients** (any PC, `pip install -r requirements-control.txt`):
```bash
python clients/qs.py status
python clients/qs.py plans
python clients/qs.py add rt_sweep start_k=10 final_k=2 time_min=30 current_a=1e-6 'detectors=["@lockin"]'
python clients/qs.py start
python clients/qs.py watch
python clients/export_run.py -1                  # latest run -> CSV (one file per stream)
```
Interactive instead of the queue (on the control host): `cd control && python -i -m fastq.startup`, then `RE(iv_curves(temperatures_k=[2, 4]))`.

## Connecting the PCs

Installing an IOC PC (Windows service / Linux systemd, firewall, `EPICS_CA_ADDR_LIST`) and the control host as a
service: [deploy/README.md](deploy/README.md). Analysis notebooks: [notebooks/README.md](notebooks/README.md).
CI: `.github/workflows/ci.yml` (framework + supervisor tests, simulator plans) once the repository is on GitHub.

- **Inside the lab LAN** Channel Access works out of the box (broadcast). If a PC has several network cards,
  set `EPICS_CA_ADDR_LIST=192.168.1.255` and `EPICS_CA_AUTO_ADDR_LIST=NO`. Windows firewall on the IOC host:
  allow `python.exe` (UDP 5064/5065 and TCP).
- **From outside the LAN** (office PC, e.g. the dev PC via the router <lab router>): Channel Access does not
  cross NAT (random TCP ports, UDP search). Use the TCP services through the SSH port that is already forwarded:
  ```bash
  ssh -N -L 8088:127.0.0.1:8088 -L 60615:127.0.0.1:60615 -L 60625:127.0.0.1:60625 -L 8000:127.0.0.1:8000 <lab-ssh-host>
  ```
  then open http://localhost:8088 (all dashboards - the web server is the gateway to CA, so this one port is
  enough for the browser) and run `clients/qs.py` / `clients/export_run.py` locally as if on the control host. For raw PV access from
  outside, work on the IOC host (SSH / remote desktop) or add an EPICS CA gateway on the IOC host later.
- Data server API key: `FASTQ_TILED_API_KEY` (generated on first start, stored next to the data).
- Dashboard accounts (optional, recommended): `cd control && python -m fastq.users add <name> --role admin`
  (the first one; then more on the System page). Roles viewer / operator / admin; with accounts a protected
  control asks for the user's own password, ELOG posts and queue items carry the user, and every change is
  logged in `<data>/audit.jsonl` (System page). Without accounts the dashboard is open and protected controls
  use the lab PIN. The AI tools log in as `FASTQ_AI_USER` / `FASTQ_AI_PASSWORD`.

## Web dashboards (control/web, port 8088)

| page | what |
|---|---|
| Control room | the run in progress: progress, ETA, current position, held-by-suspender notice, Pause / Stop / Abort; its plots, live; lab values by role; camera live views; quick ELOG note |
| Experiments | plans grouped by setup with search, typed forms (device lists, checkboxes), presets (saved per plan for everyone), fill from an earlier run or a queue item; **scan builder** (any axes x any detectors, pulser, ROI ratios, point count); queue with reorder / edit / remove, history, console |
| Runs | search (plan, comment, sample, kind, number), run view with the same plots, camera frame viewer, CSV / HDF5 export, comment to ELOG, run again; select several runs -> overlay compare |
| Devices | per IOC host, generated from each IOC's schema (docs, units), trends, waveform plots, camera view, controls |
| Logbook | new entries with screenshots / files (also as reply in a run's thread), outbox |
| System | control-host services, storage, directories, every IOC's health + last error, IOC restart |

Top bar: sample + ELOG notebook (click to change), running run, queue state, 🔔 desktop notification when a
run ends or is held.

**Plots** are specs (`fastq/analysis.py`): x, y (glob patterns allowed), one curve per value of a column
(`group`, e.g. pump shutter), `average` (mean ± s.e.m. of equal x - repeats collapse), `combine` (pumped −
unpumped, ratio, relative), heatmap (x, y -> z). Plans describe themselves in `md["plot_hints"]` (x, signals,
group, average, map_y) and `fastq.analysis.default_plots` builds every run's plots from that (one place); ✎ edits a plot, "Save layout for this plan" stores the set for every later run of that plan
(`<data>/plot_layouts.json`). The same specs work in notebooks and for the AI tools:

```python
from fastq.analysis import load_run      # cd control (or sys.path), FASTQ_LAB=...
run = load_run(905)                      # scan_id, -1 = latest, or uid prefix
df = run.table()                         # pandas, scalar columns
run.plot({"x": "delay", "y": ["cammain_over_camref_HE"], "group": "pump_shutter", "combine": "diff"})
frames = run.frames("cammain_image")     # camera frames from the camera PC's HDF5, via Tiled
```

The browser talks only to the web server (HTTP + websocket); it reads PVs over Channel Access for it. PV writes
happen only on an explicit click, only for the controls of a device's profile (schema or
`clients/device_profiles/*.json`) plus IOC restart, and are refused while a plan runs (or when the queue state is
unknown). Each control has a safety level from its IOC: safe (written at once), confirm (a dialog) or protected
(the lab PIN, `security.pin_sha256` in lab.yaml, checked on the server; `python -m fastq.security <PIN>`).
`web/server.py --read-only` refuses every change (writes, queue, ELOG, settings).

Device pages: values, controls, and graphs - several values per graph, as many graphs as wanted, layouts
saved per device. The server records every numeric value (fastq.history: raw values for `raw_days`, per-minute
min/mean/max for `keep_days`, global on the System page, per device on its page), so graphs show the past too
and long spans are cheap. Derived values (fastq.derived: formulas of the experiment such as a two-terminal
resistance, edited on the `derived` device page) are computed live and, from the raw data, in every run. Screen capture needs a secure page (http://localhost, also through the tunnel, or
`--ssl-certfile/--ssl-keyfile`); otherwise paste a screenshot (Win+Shift+S, Ctrl+V) or attach files.

### Automatic pause (suspenders, fastq/suspenders.py)
`lab.yaml` `suspenders:` hold a running scan while a condition holds and continue when it clears, e.g.
`{signal: interlock.triggered, when: true, resume_after_s: 10}` or `{signal: vac.pressure1, above: 1e-4}`.
The interrupted point is measured again; the control room shows why the run is held.

### Alarms (fastq/alarms.py)
Rules on device values (System page; `lab.yaml alarms:` seeds them): `{ref: itc1.temp, above / below /
rate_per_min, for_s, actions: [notify, elog]}`. A rule fires after its condition held for `for_s` and clears
after `for_s / 2` back to normal; both are posted (dashboard toast + desktop notification, ELOG entry). Unlike
suspenders they never touch a run - they tell people.

### Unattended nights (fastq/plans/flow.py)
`wait_until("22:00")`, `wait_for("itc1.temp", within=[4.2, 0.05], for_s=600, timeout_s=21600)` (any value,
above / below / within) as queue items before the next one, and `sequence(steps=[{plan, kwargs, comment}],
repeats=N or 0 = until Stop)`: every step its own run (metadata `part_of: sequence`). Examples in the recipes.

### Backups (fastq/backup.py)
`lab.yaml backup: {target, every_h, keep}`: the control host copies `<data>` there - append-only folders
mirrored, databases (Tiled, history) as consistent snapshots, the newest `keep` kept; status on the System page.
`python -m fastq.backup --now` for one now.

## ELOG (control/fastq/elog.py)

- **Current experiment** = sample + ELOG notebook (one logbook per experiment): set in the top bar, stored in `<data>/experiment.json` (defaults: `metadata.sample`, `elog.default_experiment`); every new run
  records the values valid at its start. A plan can override it with
  `md={"elog_logbook": "..."}`.
- **Run entries**: every run start creates an entry (plan, parameters, sample, uid); the run stop is posted as a
  reply (exit status, duration, events per stream). Comments / custom entries for a run are replies in the same thread.
- **Attributes** (`elog.attributes`: RunN, Sample, File, Plan, Status, Category, Author, Subject): run entries and every
  reply in a run's thread carry the run number, sample, plan and raw-data file name. The logbook list and each
  logbook's Category choices are read from the ELOG server; automatic entries use `elog.auto_categories`
  (Routine entry for runs, Other for device events).
- **Login**: `elog.user`; password in `<data>/elog/password.txt` (or env `FASTQ_ELOG_PASSWORD`).
- **No duplicates**: every entry carries a hidden marker; if the server's reply lacks the entry id (some servers behind nginx do)
  or a post times out, the service finds the entry by its marker instead of posting again.
- **Device logbooks** (`elog.device_logbooks`): IOC restarts, instrument connection lost/restored, laser errors,
  Keithley errors - rate limited per device and event type.
- Everything goes through an outbox (`<data>/elog/pending|sent|failed`), posted by the ELOG service
  (`python -m fastq.elog`, started by `start_control_host.py`): runs never wait for ELOG, nothing is lost while
  it is unreachable. Configure in `lab.yaml` (`elog:`).
- Client library: PSI py_elog (installed from GitHub, see requirements - PyPI `elog` is a different package).

## Measurements (control/fastq)

Generic plans (`plans/measurements.py`, `plans/scan.py`): `nd_scan` (any axes x detectors; `period_s` makes
it a log), `rt_sweep`, `iv_curves`, `aux_waveform`, `set_temperature`, `laser_sequence_scan` (sequence of
reference / pulses / ramp / wait steps x scan dimensions (nested / randomized) x temperatures, resistance
monitored continuously), `log_until`. Fixed-parameter versions (R(T), I-V, lock-in frequency /
amplitude sweeps, temperature / lock-in logs, ...) are recipes in `experiments/presets.yaml`. Plans and
recipes that must stay out of this public repository live in a private experiments folder
(`../FastQ-private/control/experiments` or `FASTQ_EXPERIMENTS=<dir>`, same layout: `*.py` plans,
`presets.yaml` recipes, optional `migrate_rules.py`); they load and are archived like the public ones.
Plan parameter conventions: `control/experiments/README.md`.

Run numbers (`scan_id`) never reset: a counter file `<data>/scan_id.txt` (continues after the highest number in
the JSONL files). Every run records its complete plan call (`plan_request`: all parameters incl. defaults, devices by
name), the role devices (`roles`) and the code version (`code`: all plan + library code is archived once per version
in `<data>/code/<version>.zip`). The ELOG run entry shows the call and has the call (JSON) and the plan's source file
attached. Dashboard: Runs page "Run again with these parameters" / Queue page "fill parameters from an earlier run";
if the plan was removed or changed since, "Restore the exact version" puts the archived plan back as
`<plan>_<version>` (file `control/experiments/restored_*.py`). CLI: `python -m fastq.provenance show <uid>` /
`extract <version> <dir>`.

Data per run: `primary` stream (discrete readings) and `<signal>_monitor` streams (every IOC update with its
EPICS timestamp: resistance, temperature, laser readbacks, and the `phase` / `condition` / scan-dimension
bookkeeping). Stored in Tiled and, independently, as JSONL in `data.backup_dir`. New rows reach Tiled every
`data.flush_s` seconds (default 2, written from a background thread) - the dashboard plots runs live.
Where (control host, `data.backup_dir` in lab.yaml; also listed on the dashboard System page):
`<data>/jsonl/<date>_<plan>_<uid8>.jsonl` = the raw Bluesky documents of each run, complete and readable
without any server; `<data>/tiled/tables.sqlite` (event tables), `catalog.db` (run metadata), `files/` (arrays)
= the Tiled data server; `<data>` is the parent of `data.backup_dir`.

Adding / removing plans: see `control/experiments/README.md` (drop in a file, then "Reload plans" on the Queue page).

Adding an instrument: see [docs/ADDING_A_DEVICE.md](docs/ADDING_A_DEVICE.md) - one declarative class
(`ioc_host/common/devkit.py`) per instrument; the ophyd device (`fastq.generic`) and the dashboard profile
(`fastq.profiles`, with a control for every writable param and action) are generated from the schema
the IOC publishes. Every instrument works this way, including the ITC503, Keithley, MFLI and Carbide;
`clients/device_profiles/<type>.json` can still override a generated profile.

### General scan: `nd_scan` (fastq/plans/scan.py)
Nested axes, outermost first; each axis is any settable device (or one of its params), a `zip` of several,
or a `repeat` counter (`repeat: 0` = until "Stop", which ends the run with status success). Moves are
set-and-confirm (the device's declared tolerance / settle time / `moving` flag, overridable per axis), inner
axes are re-applied after an outer move (old 4dmag sequencer semantics), `snake` reverses alternate passes.
Every point triggers all detectors together; with `pulser` (a device with a Trigger action) the cameras are
armed and the pulser fires until every frame arrived. `ratios` divides ROI values of two cameras per point.
`monitors` are recorded continuously. Plans take devices by name; names inside axis dicts are resolved too.

### Cameras (ioc_host/common/camera.py, fastq/detectors.py)
A camera IOC keeps full frames in HDF5 on its own PC (`--data-root`, one file per run and camera,
`/entry/data/data`, SWMR) and publishes spectrum, ROI values and a live image as PVs. Runs carry one scalar
column per ROI (`<cam>_<roi>`) and a reference to the frames (`<cam>_image`, StreamResource/StreamDatum),
which Tiled serves when the folder is in `data.readable_storage` (a share once the camera PC is another PC:
give the camera IOC `--data-uri file://<host>/<share>`). `LiveView` = continuous frames for alignment, nothing saved.

### 4DMAG (experiments/fdm.py)
`fdm_static` (repeat until stop), `fdm_background` (BG, pump closed), `fdm_delay_scan` (delay segments as
in sequence.py), `fdm_waveplate_scan`, `fdm_rotation_scan`, `fdm_field_loop`, `fdm_fluence_scan` (ND wheel,
pump closed while it moves), `fdm_set_rois` (Integrators.csv -> both cameras, 4dmag preprocessing: mean -
TargetCounts - background slice; main/ref ratio per ROI in every event), `fdm_live`. Every plan takes
`comment="..."` - the run description that goes to the ELOG entry (as 4dmag's run info did).

### Several IOC PCs
Each IOC PC runs the supervisor with its own host file (`start_ioc_host.bat ioc_host\hosts\<pc>.yaml`)
and its own directory IOC prefix; directories list each other as peers (`directory_ioc.py --peers`), so a client
still needs one directory name. Device prefixes must be unique lab-wide. Try it:
`python sim/sim_stack.py --devices sim/sim_host2.yaml --directory TEST:DIR:H2: --peers TEST:DIR: --no-carbide`.

## AI access (control/fastq/mcp_server.py)

An MCP server gives AI assistants (Claude Desktop / Code or any MCP client) the lab: overview, device
self-descriptions and live values, plans with their parameters, queue, runs and their data, ELOG. It talks
only to the dashboard API, so it can do no more than a person in the browser; `FASTQ_AI_ACCESS` = `read`
(default) / `queue` (add queue items, ELOG notes) / `control` (start/stop the queue, dashboard controls).
`lab.yaml` `ai: {mcp_port: 8089, access: read}` starts it with the control host (HTTP, `/mcp`); a local
client can also start it itself over stdio (example config in the module docstring). Run metadata, plan
docstrings, parameter docs and units are what the assistant reads - keep them meaningful. It also reads the
notebook (`elog_read`), value history (`value_history`), alarms, derived-value definitions and the recipes,
and queues a recipe with changes (`queue_recipe`) - so it can propose the next measurements from what was
written and measured; with dashboard accounts it logs in as `FASTQ_AI_USER` and the audit log shows its actions.

## Renames of 2026-10-27 (python -m fastq.migrate)
Instrument PV names, plan parameters and some plans were renamed (HANDOFF.md). `cd control && python -m
fastq.migrate` shows what changes in the saved presets, plot layouts, device views, derived values and the
value history; `--apply` rewrites them (backups `*.bak`) - run it before the IOCs restart with the new names.
"Fill from run" translates the parameters of older runs by itself.

## Testing without hardware

```bash
python sim/sim_stack.py                                   # simulated IOC host (TEST:DIR:, TEST:ITC1:, ...)
python tests/run_sim_plans.py                             # the measurement plans against it
python sim/mock_elog_server.py --port 8091 --user daq --password simpw   # stand-in ELOG server (browse it)
python -m pytest tests/test_framework.py -q               # framework tests (start their own mock IOCs)
FASTQ_ELOG_PASSWORD=simpw python control/start_control_host.py --lab control/lab_sim.yaml   # full stack on the simulator
```

Instrument notes: `zhinst-core` must match the LabOne server version; the MFLI data server has few client
slots (the IOC should be the only LabOne client); Keithley pass-through replies are read to GPIB EOI and the
6221 needs ~0.5-1 s between delta ARM and INIT. Details in `HANDOFF.md`.
