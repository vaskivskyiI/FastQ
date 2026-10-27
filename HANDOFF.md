# Handoff - FastQ EPICS/Bluesky stack (state 2026-10-26)

Architecture, PC roles, networking, start-up: see README.md. This file keeps
the current state, open issues, and lessons learned the hard way.

## Machines
- **transport PC** = IOC host (+ control host) of the transport setup (Python 3.14).
  GPIB: 6221 GPIB0::18, ITC503#1 ::23, ITC503#2 ::24, IPS120-10 magnet PSU ::25.
- **dev PC** = office / development PC. Dev venv `.venv-dev` (Python 3.13).
- **4DMAG PC** = 4DMAG IOC + control host (`ioc_host/hosts/4dmag.example.yaml`, `control/lab.4dmag.example.yaml`).
  Not yet deployed. Needs Python <= 3.12 (Nanotec NanoLib), Jsdk runtime for the SI cameras, APT.dll.
- **lab router** = SSH way into the lab LAN (forwards to the transport PC, the MFLI and the Carbide).
  Channel Access does not cross it; use the SSH tunnel for the queue/data servers (README).
- Host names, user names, real addresses: not in this public repository (keep them in the PCs' own
  config files and a private notes file, e.g. HANDOFF.local.md, which git ignores).

## State 2026-10-26: universal / decentralised extension (all on the simulator, nothing on hardware yet)
- Declarative instruments (`ioc_host/common/devkit.py`, guide `docs/ADDING_A_DEVICE.md`): one class per
  instrument -> IOC + `IOC:SCHEMA`; control host builds ophyd devices (`fastq.generic`) and dashboard profiles
  (`fastq.profiles`) from it. 27 IOC types; all declarative ones have simulators and pass
  `tests/test_framework.py` (schema of each + generic device / camera / scan tests).
- Cameras (`ioc_host/common/camera.py`, `fastq.detectors`): HDF5 per run on the camera PC, StreamResource
  docs, Tiled serves the frames (`data.readable_storage`); ROI values as scalar columns; LiveView.
- `nd_scan` (`fastq/plans/scan.py`) = general scan; 4dmag recipes `experiments/fdm.py`, transport recipes
  `experiments/transport.py`. Every plan takes `comment=` (run description -> ELOG subject/text).
- Multi-PC: per-host supervisor files (`FASTQ_IOC_CONFIG`, `start_ioc_host.bat <file>`), peered
  directories (tested with two simulated hosts). The supervisor no longer auto-starts IOC types it has no
  address for.
- AI: `fastq/mcp_server.py` (MCP over the dashboard API; access read/queue/control). Tested end to end on the
  simulator: queue_add -> queue server -> Tiled (incl. camera frames) -> ELOG entry -> run_data.
- Simulator: `sim/sim_devices.yaml` = 30 processes / 28 devices (transport + 4dmag); `tests/run_sim_plans.py`
  9/9; transport and 4dmag recipes run.

### Dashboard (2026-10-27 rewrite, control/web/static: plot.js, core.js, panels.js, pages.js)
- Pages: Control room, Experiments (plans + scan builder + presets + queue), Runs (search, run view,
  frames, export, compare), Devices, Logbook, System. Plots are specs evaluated by fastq.analysis
  (server /api/runs/{uid}/plot); layouts per plan in <data>/plot_layouts.json, presets in <data>/presets.json,
  live run in <data>/live.json (written by the RunEngine - the web server must see the control host's data dir).
- Plans write md["plot_hints"] {x, signals, group, average, map_y}; fastq.analysis.default_plots builds the
  plots of every run (and the live run) from them - the one place plots are made.
- Suspenders (lab.yaml suspenders:) pause runs on interlock / vacuum / flow conditions.
- Removed: clients/monitor_panel.py (Tk), old app.js / chart.js. The supervisor opens the dashboard instead.
- 2026-10-27: itc503, keithley, mfli, carbide converted to devkit (same PV names; control/fastq/devices.py and
  their JSON profiles removed; every device is now built from its schema). Verified on the simulator only -
  NOT YET ON HARDWARE. Changes a hardware test must cover: ITC remote/heater-auto on setpoint and ramp; MFLI
  demod sample + settings polled through one lock; Carbide setpoints confirmed by readback (shutter 15 s,
  attenuator/pulse duration 60 s); Keithley run loop now a worker thread (Acquire exclusive, Abort/Stop
  unlocked, on_exit stops the source). Attribute renames in plans: laser.shutter -> shutter_open,
  pp_enable_cmd -> pp_enable, lockin.sigout_amp -> sig_out_amp, keithley.acquire/abort/stop_run ->
  acquire_cmd/abort_cmd/stop_cmd, pd_interval -> pd_interval_plc, dc_settle -> dc_settle_s, itc.ramp_start ->
  ramp_start_cmd; the ITC readback column is now "itc1" (was "itc1_temp"), like every positioner.
- Dashboard controls are generated for every writable param and action (they were missing for devkit devices).

### Operations round (2026-10-27, fourth round)
- fastq.migrate: one table of old -> new names; `python -m fastq.migrate [--apply]` for presets, layouts,
  views, derived.json, history.db (two-phase PV rename); run_request translates older runs on the fly.
- fastq.users: accounts (viewer / operator / admin, PBKDF2, HMAC session cookie, 12 h), audit log
  <data>/audit.jsonl (every mutating request + PV writes with details + logins), protected controls ask
  the user's own password; no accounts = open dashboard + lab PIN. Queue items / ELOG posts carry the user.
  AI tools log in as FASTQ_AI_USER. Basic-auth env vars removed.
- fastq.alarms: rules (above / below / rate, for_s, hysteresis) evaluated in the web server on PV updates;
  notify (toast + desktop) and ELOG; System page editor; top-bar chip.
- fastq.plans.flow: wait_until, wait_for, sequence (steps keep their own run md, part_of: sequence;
  recorded() lets the innermost recorded plan win, queued_plan key).
- fastq.backup: start_control_host helper (backup.target); mirror + SQLite backup-API snapshots, keep N.
- AI tools: elog_read (/api/elog/entries), list_recipes, queue_recipe, value_history, alarms, derived_values.
- Quiet exit: caproto logger silenced from threading's exit hooks (fastq.discovery).
- IOC-host service / installer, CI workflow and analysis notebooks: see deploy/, .github/, notebooks/.

### Devices, history, safety, derived values (2026-10-27, second round)
- Device page: graphs with several values each, "📈" on a value = new graph or add to an existing one;
  layouts saved per device (<data>/views.json, /api/views/device/{name}). Graph data = fastq.history
  (<data>/history/history.db, recorded by the web server for every numeric readable param: raw rows at
  most every period_s when changed + per-minute min/mean/max of every update; raw_days / keep_days
  retention, global on System, per device on its page) + live updates. Long spans show the envelope.
- Control safety (devkit `safety=` on every writable Param/Action): safe = written at once, confirm =
  dialog, protected = lab PIN (security.pin_sha256, checked by the server, 5 wrong -> 60 s lockout;
  security.overrides "dev.PV" per lab). AI tools can never write protected controls. Writes are refused
  while a plan runs, when the queue server state is unknown, or a live run is in live.json (interactive
  session). --read-only refuses every mutating route. Long actions (timeout_s > 20 s) are started
  without waiting (reply "started"). Inputs select on focus; Enter presses the field's button.
- Derived values (fastq.derived, lab derived:, <data>/derived.json, edited on the "derived" device page):
  experiment formulas over "device.attr" inputs + constants, validated (no attribute access, whitelisted
  functions). Live: ioc_host/iocs/derived_ioc.py on the control host (restarted by start_control_host
  when the file changes). Runs: md["derived"] (definitions + data keys); fastq.analysis computes the
  columns per row (inputs from other streams interpolated, config values as constants) - plots, CSV,
  HDF5 get them. The MFLI no longer has Resistance / SeriesR: rt_sweep and the laser scans monitor X, Y,
  SigOutAmp; set_excitation takes the series resistor from the derived constant series_r.
- Drivers extended by four agents from manuals / vendor code (reports summarised in the commit):
  cameras (SI: cooling, binning, ROI, cleaning, shutter; TSI: gain, black level, hot pixels, frame
  rate, EEP; Abort reaches a running exposure; DetectorState; devkit --also = several instruments per
  process), lasers/motion, cryo/magnet/vacuum/flow (Voegtlin CloseValve wrote the wrong code - fixed;
  IPS120 persistent mode; LS336 per-input alarms; TPG366 switching functions), electrical (Keithley wave
  mode, ranges, 2182A settings, PdLowMeas 1|2 fix; MFLI full node set; SR830 buffer; AMETEK 7270; DAQ AO).
  NONE of the new driver code has run on hardware.
- Audit refactors: session.resolve() is the one device lookup (recipe_tools.device/optional_role and
  scan.resolve removed); fastq.plans.keithley (configure_delta/pulse_delta, record_sweep); nd_scan
  safe_state= (fdm closes the pump, transport aborts the Keithley); temperature sweep waits time out;
  /api/run/{stop,abort} pauses first (dashboard + MCP); data.backup_dir required; session.tiled_dir /
  tiled_api_key; supervisor never auto-adds devices or rewrites host files; ioc_host/common/naming.py
  (prefixes incl. --also, device names) for directory + supervisor; device events watch every IOC's
  LastError and new devices; restored plans go to <data>/restored (button "Restore exact code" in the
  run view); code archive includes data files of experiments/; fastq/roles.py warns when a device cannot
  do what its role needs; schema ophyd hints declare stop / pulser (no name guessing); dead code removed;
  control/lab.example.yaml lists every key; tests/run_sim_plans.py (renamed) also runs tm_* and fdm_*.
- Third round (same day): every audit item done, NOT backward compatible:
  * PV names per instrument class (fastq/roles.py docstring). Lock-ins: MFLI OscFreq -> Frequency,
    TimeConst -> TimeConstant, SigOutAmp -> Amplitude, PhaseShift -> Phase, RefMode -> RefSource,
    AuxOut0..3 -> AuxOut1..4, AuxIn0/1 -> AuxIn1/2 (front-panel numbers); AMETEK Slope -> FilterSlope,
    RefPhase -> Phase, OscFrequency -> Frequency, OscAmplitude -> Amplitude, DAC1-4 -> AuxOut1-4,
    ADC1-4 -> AuxIn1-4, Reference -> RefSource. Temperature: ITC SweepStatus -> SweepStep + Ramping; the
    Lake Shore 336 got Temp / Setpoint (positioner) / Heater / Ramping / RampTarget / RampTime /
    RampStart / RampStop for the --loop loop (per-loop PVs kept, no longer positioners). Lasers: Pharos
    ShutterOpen / PPEnable writable (EnableOutput / EnablePP actions removed), PulseCount -> BurstCount,
    PPTriggerSource -> TriggerSource, Status -> GeneralStatus. Motors: Speed -> Velocity, Accel ->
    Acceleration (Xeryon, Arduino steppers). Derived inputs: mfli1.sig_out_amp -> mfli1.amplitude.
  * Plan parameters: role-named devices (temperature=, lockin=, laser=, keithley=, probe=), unit
    suffixes, settle_s / tolerance_k, aux_index 1-based (control/experiments/README.md, checked by
    test_plan_conventions). Removed plans -> recipes (control/experiments/presets.yaml, dashboard
    "recipes"): temperature_log, tm_dc_iv (= iv_curves), ... nd_scan period_s makes it a log.
    Project-specific plans / recipes: private experiments folder (README "Measurements").
  * Run md: run_kind always (recorded() default = plan name), detectors / motors instead of devices,
    no plan_name / setup in building blocks (setup comes from lab metadata).
  * Host files: supervisor keys = device names (itc1, cammain ...); ioc_script required. Simulators of
    ITC503 / Keithley / MFLI in ioc_host/drivers/*_sim.py. ELOG device events from schemas (devkit
    Param(event=...)); the poster survives a bad entry. Camera event arrays declared (ophyd
    event_arrays). Tests: plan conventions, recipes vs signatures, every dashboard /api call exists.
- On the dev PC: something else listens on 127.0.0.1:8000 - lab_sim uses Tiled :8010.

### Unverified on hardware - check at first use (details in each IOC's docstring)
- SI cameras (Jsdk via ctypes, prototypes from the C docs): camera index base (4dmag used 1, 2), two IOC
  processes sharing one Jsdk/EDT install (4dmag used ONE process for both - if it fails, devkit needs a
  multi-instrument IOC), acquisition mode 2 vs 3 (`--acq-mode`; production used 2), trigger/shutter
  polarity values, chdir into `--jsdk-path`.
- Nanotec rotator: `--scale` (units of 0x607A, default 0.1 deg), homing mode; Python <= 3.12.
- Arduinos: opening the port resets the board (DTR). Shutter/stepper settings are re-sent in open() as 4dmag
  did. The interlock firmware does not drive pin 7 in setup() - after a reset it reads "not tripped" even
  if it was (fix in firmware). 4dmag origin/main getStatus() resets the external device - not ported.
- DSC magnet PSU: `--max-current`; Voltage treated as unsigned limit (4dmag multiplied by polarity).
- Delay line: t0 293.9277 mm / direction -1 from devices.py; tolerance 0.005 ps (`--tolerance`).
- Pharos: same Light Conversion REST as Carbide (v0, :20022); error/warning PVs since 2026-10-27 (endpoints from
  the CARBIDE docs, blank where a PHAROS answers 404). APT / Nanotec Home are non-blocking (Moving + Stop).
- Mercury iTC: legacy ITC503 protocol default (as TransportMeasure); `--protocol scpi` untested. GPIB ::24
  is listed both as TransportMeasure's ITC and transport PC's itc503_2 - run only one IOC per address.
- Keithley PulseSweep / SinglePulse (from TransportMeasure's sweep_sett / send_pulse); the existing
  PulseDelta check allows LME 2|3, TransportMeasure uses 1|2 - check the 6221 manual.
- Lakeshore 336, SR830, TSI camera, NI DAQ, AMETEK lock-in, TPG366, Xeryon, IPS120, SDG: protocol code from
  manuals / 4dmag, no hardware run. Raspberry Pi MUX: pin numbers must be filled in at the rack.
- Not ported: iLustra (DDE), the "unified device manager" of 4dmag origin/iv_redis.

### Lessons 2026-10 (keep)
- Tiled's SQL table store holds no array columns: event arrays must be omitted or split into scalars
  (cameras: one column per ROI); frames go through StreamResource.
- CA clients never see a refused put: devkit records it in LastError/ErrorCount and fastq.generic fails the
  set() at once (read ErrorCount BEFORE the put). Limits are enforced by devkit, not as caproto ctrl limits.
- EPICS EGU <= 8 characters, ENUM <= 16 states (devkit checks / truncates).
- Asking a Tiled stream for each column's shape costs one HTTP request per column (~0.7 s on sqlite):
  use the stream's metadata data_keys.
- On the dev PC ports 6379 (WSL Redis) and 8090 are taken: lab_sim uses fakeredis :6380, mock ELOG :8091.
- MCP Python SDK 2.x renamed FastMCP -> MCPServer (mcp_server.py supports both).

## State 2026-09-27
- IOCs follow EPICS conventions (X / X_RBV, EGU, iocStats, IOC:TYPE); directory IOC
  FASTQ:DIR: + ophyd discovery; Bluesky queue server + Tiled on the control host.
- Tested on the simulator (`sim/sim_stack.py`, `tests/run_sim_plans.py`): all
  measurement plans pass; queue server + Tiled end-to-end (see below).
- transport PC migrated 2026-09-27 (old tree in `backups\pre_bluesky_20260927_110055`).
  `tests/hw_readonly_test.py --skip itc`: 31/31 on the real Keithley, MFLI, Carbide
  (new PV names, iocStats, directory -> discovery -> ophyd reads, instruments unchanged).
  Control host (queue + Tiled) ran on transport PC; a laser sequence plan (MFLI resistance during
  laser pulses + an energy ramp, attenuator <= 5 %, divider >= 10000) succeeded via the queue.
- Laser limits are per run only: `laser_sequence_scan(laser_limits={...})` (recorded in the run md);
  No permanent limits.
- Web dashboards (control/web, :8088) + ELOG (control/fastq/elog.py, outbox + service) - tested on the
  simulator with sim/mock_elog_server.py (run thread start/comment+screenshot/stop, device events on IOC
  restart, notebook switch, login). Not yet on transport PC / the real ELOG: fill lab.yaml `elog:` (URL,
  user, logbook names) and set FASTQ_ELOG_PASSWORD.
- ITC503 #1 and #2: GPIB interface hung 2026-09-26 22:40 (serial poll OK, every
  write VI_ERROR_IO; PC reboot did not help) - needs a check at the rack.

## Lessons (keep)
- Never let a scanned readback PV have a putter (caproto `instance.write` runs the
  putter; bool PVs arrive as "Off"/"On" and bool("Off") is True -> the laser shutter
  opened). Readbacks read-only, setpoints separate, seed setpoints with
  `write(..., verify_value=False)`. No instrument writes at IOC startup.
- caproto on Windows: several IOCs on one host all got TCP 5064 (SO_REUSEADDR) ->
  `ioc_host/common/ca_windows.py` (exclusive bind). CA clients never see a rejected
  put (it just never completes) -> devkit records refusals in LastError/ErrorCount and fastq.generic fails
  the put at once (exclusive actions such as Keithley Acquire are refused while one runs).
- caproto int PV with only lower_ctrl_limit gets upper limit 0 -> set both.
- zhinst-core must equal the LabOne server version; the MFLI data server has ~8
  client slots and the LabOne web UI held 7 -> one API client at a time.
- Keithley 6221/2182A: read pass-through replies to GPIB EOI (they end "\n\n");
  >= 0.5 s between SOUR:DELT:ARM and INIT (IOC ArmSettleS = 1 s); "...recommended"
  SYST:ERR codes are warnings.
- bluesky-queueserver on Windows: use --startup-module (the startup-dir mode puts
  a Windows path into Python source unescaped); Redis via fakeredis TcpFakeServer.
- Windows/py3.14: awkward crashes if numba/numexpr load msvcp140 first -> control/tiled_server.py and
  fastq/__init__ import awkward first (real fix: update the VC++ Redistributable on transport PC).
- Tiled writable catalog needs a table store: `-w sqlite:///.../tables.sqlite` (duckdb<1.4, pinned by
  tiled 0.2.18, has no Python 3.14 wheel - transport PC installs tiled with --no-deps + its deps minus duckdb).
- py_elog sends latin-1 only: entries are posted as HTML (`<pre>` + entities) so Ω, µ, ° survive.
- tiled client slicing: negative slices need step -1 (`client.items()[-1:-41:-1]` = newest 40).
- IOC:SYSRESET: the IOC exits before confirming the put -> write without wait. sim_stack.py stops the
  whole simulator when one IOC exits (the real supervisor restarts it).
- Carbide IOC putters ran their REST calls concurrently: "close shutter" then "open shutter" put back to
  back executed as EnableOutput, CloseOutput (found 2026-09-27 on the simulator, plan hung). All laser
  writes now go through one lock (commands execute in put order; devkit does this for every
  instrument), and the ophyd laser signals time out (positioner timeout_s 15 s / 60 s) instead of
  waiting forever.
- MFLI IOC wedged 2026-09-27 15:57 during an aux_waveform ramp (AuxOut put every 50 ms while the poll
  read the demod in another executor thread): all PVs froze, the plan waited forever for AuxOut0_RBV.
  Now every LabOne call (poll + puts) goes through the devkit instrument lock, and an AuxOut set
  completes when the IOC finished the put. Symptom to recognise: PV timestamps frozen while updates keep arriving.
- Plans get devices by NAME (mfli1), never by PV prefix - a prefix reaches the plan as a str
  ('str' object has no attribute ...). The dashboard form offers the names; the web server maps prefixes.
- TiledWriter batch_size=200 (default 10000 = nothing visible in Tiled until the run ends).
- Tiled live writing: fastq.session.live_tiled_writer (TiledWriter's per-run writer flushed every
  data.flush_s = 2 s, inside bluesky's BufferingWrapper thread). Uses bluesky 1.15 private _RunWriter.
- Queue server: a FAILED plan is pushed back to the front of the queue - clear it before starting
  the next one (otherwise the failed plan simply runs again).
- bluesky list_scan cannot be queued (queue-server validation of *args contradicts list_scan's own
  check) - removed from the startup exports; use scan.
- Laser pulse duration: moving the compressor is fine (221 fs in the recipes is the rounded
  shortest pulse; the laser reports -220.8). A 5 % attenuator request reads back 5.098 % (quantisation).
- Plans live in control/experiments/*.py (auto-loaded by fastq.plan_registry; README there).
  start-re-manager runs with --update-existing-plans-devices ENVIRONMENT_OPEN, so "Reload plans"
  (= environment close/open) picks up new/removed plans without restarting the control host.
- Overview dashboard: built from the directory; each type's key fields/trend = the params marked
  overview=/trend= in its IOC (or a clients/device_profiles/<type>.json override).
- Every plan from control/experiments is wrapped (plan_registry.recorded): its runs carry
  md plan_request = {name, kwargs} (devices by name, *args as "args") -> dashboard "fill from run".
  Runs before 2026-09-27 20:00 only have the queue history (lost when fakeredis restarts).
- Sample / notebook: fastq.experiment state (<data>/experiment.json) injected into every run by a
  RunEngine preprocessor (plan md wins); ELOG attributes via elog.attributes mapping.
- ELOG: URL, logbooks and user in the lab file; password in <data>/elog/password.txt (or env
  FASTQ_ELOG_PASSWORD). The lab server's certificate is signed by a private CA; py_elog posts with
  verify=False (encrypted, not verified).
- The lab ELOG (behind nginx) answers a submit WITHOUT the Location/id py_elog expects -> py_elog raises
  although the entry was created; retries made duplicates.
  Now: hidden marker <!-- fastq-entry:<id> --> in every entry, looked up after an unclear post and
  before every retry. Server charset is UTF-8: text sent as ASCII + HTML entities, attribute values
  as UTF-8 bytes (py_elog would send latin-1).
- Logbooks and their Category options are read from the server (fastq.elog.server_logbooks /
  logbook_options; dashboard caches 5 min). Temperature attribute deliberately not sent.
- Run numbers: fastq.provenance.persistent_scan_id_source (<data>/scan_id.txt, file lock); first run
  after the change on transport PC = 11. Code archive <data>/code/<hash>.zip written at plan load.
- iv_curves records points while the sweep runs: the Keithley IOC publishes Currents/Readings/
  Timestamps then N every 0.5 s during DCSweep; the plan triggers, then every 1 s emits new rows
  (with a checkpoint, so a deferred pause acts within ~1 s) - pause/resume keeps all points.
- Beware python heredoc edits on Windows: text-mode writes produced CRLF and "\0"/"\n" escapes
  turned into real NUL/newline characters - write files with newline="\n" and raw strings.
