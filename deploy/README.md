# Deploying FastQ hosts as services

| file | what |
|---|---|
| `install_ioc_host.ps1` / `uninstall_ioc_host.ps1` | Windows IOC PC: venv + headless IOC supervisor as a service (Scheduled Task or NSSM), firewall, `EPICS_CA_ADDR_LIST` |
| `systemd/install.sh` | Linux IOC host or control host: venv + systemd unit (`--dry-run` prints everything) |
| `systemd/fastq-ioc-host.service`, `systemd/fastq-control-host.service` | the unit templates `install.sh` fills in |

The service runs the **headless supervisor** (`ioc_host/supervisor/headless.py`): the same process manager as
the GUI, without a window. It starts the enabled devices of one host file, restarts an IOC that exits with the
GUI's back-off (2 s, 4 s, ... 60 s), writes `supervisor.log` + one rotating log per device, and stops every IOC
on Ctrl-C / Ctrl-Break / SIGTERM. On Windows it also holds its IOCs in a kill-on-close job object, so a hard
stop (Task Scheduler "End", service stop) never leaves an orphaned IOC holding a COM port or GPIB address.

```bash
python ioc_host/supervisor/supervisor.py --headless --config ioc_host/hosts/<host>.yaml [--log-dir DIR]
```

The host file is only read. After editing it, restart the service. Do not run the supervisor GUI on the same
host file while the service runs (every IOC would run twice); to use the GUI, stop the service first.

## Windows IOC PC, step by step

1. **Prerequisites**: Python **3.12** 64-bit from python.org (tick "py launcher"; 3.13/3.14 work too, except
   for a Nanotec rotator: NanoLib needs <= 3.12), git, and the instrument drivers the host needs (NI-VISA /
   NI-488.2 for GPIB, LabOne for an MFLI - `zhinst-core` in `requirements-ioc-host.txt` must equal its
   version, APT, Jsdk, ... see each IOC's docstring).
2. **Get the code**, e.g. `git clone <repo> C:\FastQ`.
3. **Host file**: `ioc_host\supervisor_config.yaml`, this PC's own file - not in git, so a `git pull` never
   overwrites it. `start_ioc_host.bat` creates it on the first start from an example of
   `ioc_host\hosts\*.example.yaml` (`4dmag`, `transport`, `minimal`; format in `ioc_host/supervisor/config.py`).
   Edit it, start the GUI (`start_ioc_host.bat`) and fix the args until every device is "running".
   Close the GUI and answer "Yes" (stop the IOCs). A good new setup: copy it back as a new
   `ioc_host\hosts\<pc>.example.yaml` and commit that, so the next PC can start from it.
4. **Dry run** in an **elevated** PowerShell (Run as administrator):
   ```powershell
   cd C:\FastQ
   powershell -ExecutionPolicy Bypass -File deploy\install_ioc_host.ps1 -HostFile ioc_host\supervisor_config.yaml -OpenFirewall -WhatIf
   ```
   It finds Python, lists the enabled devices of the host file and prints every change it would make.
5. **Install and start**:
   ```powershell
   powershell -ExecutionPolicy Bypass -File deploy\install_ioc_host.ps1 -HostFile ioc_host\supervisor_config.yaml -OpenFirewall -Start
   ```
   This creates `C:\FastQ\.venv` (the one `start_ioc_host.bat` also uses), installs
   `requirements-ioc-host.txt`, and registers the Scheduled Task **"FastQ IOC host"**: at startup (30 s delay),
   whether anyone is logged on or not, as SYSTEM, highest privileges, no time limit, re-triggered every 5 min
   (a no-op while it runs - it restarts the supervisor if it ever died). Options:
   - `-User LAB\daq` run as that account (asks for its password) - needed when an SDK, a licence or a data
     path (`--data-root` on a network share) is per user; SYSTEM cannot see mapped drives.
   - `-Python C:\Python312\python.exe`, `-VenvDir`, `-LogDir`, `-Name` (second supervisor on one PC), `-SkipPip`.
   - `-CaAddrList "192.168.1.255"` sets `EPICS_CA_ADDR_LIST` machine-wide (see below).
   - `-RemoteAddress "LocalSubnet,192.168.3.0/24"` who the firewall rules admit (default LocalSubnet).
6. **Check**:
   ```powershell
   Get-ScheduledTask "FastQ IOC host" | Get-ScheduledTaskInfo      # LastTaskResult 267009 = running
   Get-Content C:\FastQ\ioc_host\logs\<pc>\supervisor.log -Tail 30 -Wait
   Get-Content C:\FastQ\ioc_host\logs\<pc>\<device>.log -Tail 50   # an IOC's own output
   ```
   From another PC in the subnet: `caproto-get <PREFIX>IOC:TYPE` (in any FastQ venv), or the dashboard's
   System page.
7. **Operate**: `Stop-ScheduledTask "FastQ IOC host"` (stops every IOC), `Start-ScheduledTask "FastQ IOC host"`.
   After `git pull`: stop, `.venv\Scripts\python -m pip install -r requirements-ioc-host.txt`, start.
8. **Uninstall**: `powershell -ExecutionPolicy Bypass -File deploy\uninstall_ioc_host.ps1` (task/service and
   the `FastQ` firewall rules; `-RemoveVenv`, `-RemoveLogs`, `-RemoveCaAddrList` for the rest; `-WhatIf` first).

### Alternative: a real Windows service with NSSM

[NSSM](https://nssm.cc) wraps the supervisor as a service (Services console, `sc query`, recovery options).
Its stop sends Ctrl-C, so the supervisor stops each IOC itself before exiting.

```powershell
# nssm.exe (64 bit) on PATH, or -NssmPath C:\Tools\nssm.exe
powershell -ExecutionPolicy Bypass -File deploy\install_ioc_host.ps1 -HostFile ioc_host\hosts\<pc>.yaml -ServiceMode Nssm -OpenFirewall -Start
nssm status "FastQ IOC host" ; nssm restart "FastQ IOC host"
```
It sets: automatic start, working directory = repo, stdout/stderr to `<log dir>\service.out` (rotated), stop =
Ctrl-C with 60 s grace, restart on exit after 10 s. `-User` works here too.

## Network: Channel Access through the firewall and across subnets

Every IOC answers name searches on **UDP 5064** (all IOCs on a host share it) and serves its PVs on its own
**TCP** port: 5064 for the first one, a random port **49152-65535** for every other
(`ioc_host/common/ca_windows.py`). Beacons / the CA repeater use **UDP 5065**. `-OpenFirewall` adds (group
`FastQ`, profiles Domain + Private - set the lab network to Private):

```powershell
New-NetFirewallRule -Group FastQ -DisplayName "FastQ Channel Access UDP 5064-5065" -Direction Inbound -Action Allow `
  -Protocol UDP -LocalPort 5064,5065 -RemoteAddress LocalSubnet -Profile Domain,Private
New-NetFirewallRule -Group FastQ -DisplayName "FastQ Channel Access TCP (IOCs)" -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort 5064,49152-65535 -RemoteAddress LocalSubnet -Profile Domain,Private `
  -Program "<base python>\python.exe"
```
The TCP rule names the base interpreter (`home` in `.venv\pyvenv.cfg`): a venv's `python.exe` is only a
launcher that runs it, so a rule for `.venv\Scripts\python.exe` would not match the listening process. If the
control host runs on the same PC, also open its TCP ports to the clients: 8088 dashboards, 8000 Tiled,
60615/60625 queue server.

**Other subnets.** CA finds PVs by UDP broadcast, and routers do not forward broadcasts. A client (control
host, dashboard, another IOC host's directory IOC) in a different subnet needs the IOC subnet in its
`EPICS_CA_ADDR_LIST` (space separated), e.g. `EPICS_CA_ADDR_LIST=192.168.1.255` - set it on the *client* PC
(`-CaAddrList` on a Windows IOC host, `/etc/fastq/fastq.env` on Linux, or System Properties > Environment
Variables), plus `EPICS_CA_AUTO_ADDR_LIST=NO` if its own broadcast should not be searched (several network
cards). Prefer the subnet's broadcast address: a search sent to a host's own IP reaches only one of the IOCs
sharing UDP 5064 there (caproto does not forward unicast searches the way EPICS base IOCs do), and the
directed broadcast has to be allowed on the router. Where neither works (NAT, e.g. the <lab router> router)
use the web dashboard / SSH tunnel from README.md, or a CA gateway on the IOC host.

## Linux (Ubuntu) IOC host or control host

```bash
sudo apt install python3-venv          # Ubuntu's python3 has no venv module without it
git clone <repo> /opt/fastq && cd /opt/fastq
cp ioc_host/hosts/minimal.example.yaml ioc_host/supervisor_config.yaml    # this PC's files (not in git):
cp control/lab.example.yaml control/lab.yaml                               # start from an example, edit
bash deploy/systemd/install.sh ioc-host ioc_host/supervisor_config.yaml --dry-run    # look first
sudo bash deploy/systemd/install.sh ioc-host ioc_host/supervisor_config.yaml --open-firewall
sudo bash deploy/systemd/install.sh control-host control/lab.yaml --open-firewall     # control host
```
The script creates `<repo>/.venv` as the service user (`--user`, default the one running sudo; the unit adds the
`dialout` group for serial adapters), installs the requirements of the role, writes
`/etc/systemd/system/fastq-ioc-host.service` / `fastq-control-host.service`, a commented
`/etc/fastq/fastq.env` (site settings: `EPICS_CA_ADDR_LIST`, `FASTQ_TILED_API_KEY`, `FASTQ_AI_USER` / `FASTQ_AI_PASSWORD`,
`FASTQ_ELOG_PASSWORD`, ...), and enables + starts the unit. `--open-firewall` uses ufw (the same CA ports as
above for an IOC host; 8088, 8000, 60615, 60625 for a control host).

```bash
systemctl status fastq-ioc-host ; journalctl -u fastq-ioc-host -f ; ls /var/log/fastq/<host>/
sudo systemctl restart fastq-ioc-host          # after editing the host file
sudo systemctl disable --now fastq-ioc-host && sudo rm /etc/systemd/system/fastq-ioc-host.service   # uninstall
```
Stopping: the IOC unit sends SIGTERM to the supervisor only (`KillMode=mixed`), which stops its IOCs; the
control host unit sends SIGINT, which `start_control_host.py` handles by stopping its services in order. The
control host's data directory (`data.backup_dir` / Tiled in the lab file) must be writable by the service user.

## What was tested where

- `headless.py`: `tests/test_supervisor.py` on Windows (start, CA, restart after kill, Ctrl-Break stop, hard
  kill takes the IOCs along) and Linux (SIGTERM stop); also in CI.
- The installers were only run as dry runs (`-WhatIf`, `--dry-run`, also in CI) and the units checked with
  `systemd-analyze verify`; no service, firewall rule or environment variable was created by testing.
