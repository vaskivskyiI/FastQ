# Analysis notebooks

Jupyter notebooks for FastQ run data. They read from the lab's data server (Tiled) through
`control/fastq/analysis.py`, which is also what the dashboards use: the same tables, the same plot specs and the
same derived values. They are generic. Runs are picked by `run_kind` / plan name and by the columns they need,
never by a fixed uid. If a lab has no matching run, the notebook prints a clear message (`NoRun: ...`)
or skips that section.

## Start

```bash
# into the control venv (the one with requirements-control.txt)
pip install -r requirements-analysis.txt

# which lab / data server: the lab file (data.tiled_uri in it); default control/lab.yaml
set FASTQ_LAB=D:\git\FastQ\control\lab_sim.yaml          # Windows cmd
$env:FASTQ_LAB = "D:\git\FastQ\control\lab_sim.yaml"     # PowerShell
export FASTQ_LAB=$PWD/control/lab_sim.yaml               # Linux

jupyter lab notebooks/
```

Optional environment:
- `FASTQ_URL`: dashboard server for the device history (default `http://127.0.0.1:8088`); `FASTQ_USER` /
  `FASTQ_PASSWORD` if it has a login
- `FASTQ_TILED_API_KEY`: data server key, if it is not in `<data>/tiled/api_key.txt`
- `FASTQ_CONTROL`: the `control/` folder, if the notebooks are run from outside this repository

Each notebook starts with `from nbsetup import *` (`nbsetup.py` in this folder). That line finds `control/`,
connects to the data server and provides `pick_run`, `find_runs`, `recent`, `draw` (draws a `plot_data` result)
and `dashboard` (GET from the dashboard API). Choose a run at the top of a section: `None` for the newest
matching run, a scan number (`905`), `-1` for the latest run, or a uid prefix (`"1e0ba67f"`).

The notebooks are stored **without outputs**, so open and run them. Exports go to `notebooks/exports/`
(git-ignored). To check all of them headless against a running data server:

```bash
jupyter execute notebooks/run_overview.ipynb notebooks/transport_rt_iv.ipynb notebooks/4dmag_pump_probe.ipynb
```

## Notebooks

| notebook | runs it uses | what it shows |
|---|---|---|
| `run_overview.ipynb` | any | the newest runs with plan, run kind, sample, status and duration; counts and time per plan; a timeline; search by text, sample, full text or the dashboard search; one run's own record: plan call (`plan_request`), code version (`code`), roles, derived definitions, plot hints, streams and columns; a device's value history from the dashboard (`/api/history`) plotted next to the run's own readings of it |
| `4dmag_pump_probe.ipynb` | `TimeDelay` (`fdm_delay_scan`) with a pump group and ROI ratios; `BG` (`fdm_background`) | pump on / off per delay as mean ± s.e.m. (`plot_data` `group` + `average`); difference, ratio and relative difference (`combine`); main/ref ROI ratios (checked against main/ref, all ROIs as a map); background subtraction from the newest earlier BG run; camera frames and spectra of one point with the ROI ranges, pump on − off spectrum; CSV + HDF5 export (`export_hdf5`); several runs overlaid; exponential decay fit of the difference (`scipy.optimize.curve_fit`), added as a series to the plot and saved as PNG/CSV |
| `transport_rt_iv.ipynb` | `R(T)` (`rt_sweep`), `R(T) steps` (`tm_rt_steps`), `I-V` (`iv_curves`), `PulseSweep` (`tm_pulse_sweep`) | where the derived `RSample` formula comes from (`run.md["derived"]`): recomputed by hand and re-evaluated with another constant; dense R(T) from the monitor streams; cooling vs warming split; dR/dT; transition temperature (steepest slope and midpoint) and the hysteresis between the two; R(T) steps with error bars (`resistance` = Delta mean / `current_a`); I-V per curve with a linear fit for R, residuals, up/down branch difference and loop area; pulse sweeps with R per pulse |

On the simulator (`control/lab_sim.yaml`) every notebook runs. The simulated sample has no pump-probe effect
and no transition, so the fits and transition estimates there only show that the code works.
