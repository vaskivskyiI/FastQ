@echo off
REM SIMULATOR: the whole lab without hardware on this PC - a mock ELOG server, the simulated IOC host
REM (sim\sim_devices.yaml: 28 devices) and the control host with control\lab_sim.yaml, each in its own
REM window; the dashboard opens at http://localhost:8088 (protected controls: PIN 1234).
REM Close the three windows (or Ctrl-C in each) to stop.
REM
REM Virtual environment: .venv-dev (requirements-ioc-host + control + dev), created / updated automatically.
cd /d "%~dp0"
call tools\ensure_venv.bat .venv-dev dev requirements-ioc-host.txt requirements-control.txt requirements-dev.txt || goto fail

start "FastQ sim: mock ELOG :8091" "%FASTQ_PY%" -u sim\mock_elog_server.py --port 8091 --user daq --password simpw
start "FastQ sim: IOC host" "%FASTQ_PY%" -u sim\sim_stack.py
echo Waiting for the simulated IOCs (directory TEST:DIR:) ...
"%FASTQ_PY%" sim\wait_for_directory.py TEST:DIR: 90
if errorlevel 1 echo (the directory did not answer within 90 s - starting the control host anyway)
set FASTQ_ELOG_PASSWORD=simpw
start "FastQ sim: control host" "%FASTQ_PY%" -u control\start_control_host.py --lab control\lab_sim.yaml
timeout /t 15 /nobreak >nul
start "" http://localhost:8088
exit /b 0

:fail
echo.
echo start_simulator failed - see the messages above.
pause
exit /b 1
