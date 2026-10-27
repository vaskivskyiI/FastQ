@echo off
REM CONTROL HOST: data server (Tiled), queue server, dashboards (http://localhost:8088), ELOG service,
REM derived values, backups, AI access - everything of control\start_control_host.py.
REM
REM     start_control_host.bat                        this PC's control\lab.yaml
REM     start_control_host.bat control\lab_sim.yaml   another lab file
REM
REM control\lab.yaml is this PC's own file (not in git, a git pull never touches it). On the first start
REM it is copied from an example (control\lab.transport.example.yaml, lab.4dmag..., lab.example.yaml).
REM
REM Virtual environment: .venv-control (requirements-ioc-host.txt + requirements-control.txt: the
REM derived-values IOC runs here too), created / updated automatically. ELOG password: set
REM FASTQ_ELOG_PASSWORD before, or put it in <data>\elog\password.txt. Ctrl-C stops everything.
cd /d "%~dp0"
call tools\ensure_venv.bat .venv-control control requirements-ioc-host.txt requirements-control.txt || goto fail
set "LAB=%~1"
if "%LAB%"=="" set "LAB=control\lab.yaml"
"%FASTQ_PY%" tools\local_config.py lab "%LAB%"
if errorlevel 2 goto edit
if errorlevel 1 goto fail
start "" http://localhost:8088
"%FASTQ_PY%" -u control\start_control_host.py --lab "%LAB%"
if errorlevel 1 goto fail
exit /b 0

:edit
echo.
pause
exit /b 1

:fail
echo.
echo start_control_host failed - see the messages above.
pause
exit /b 1
