@echo off
REM TESTS without hardware:
REM
REM     run_tests.bat          unit tests (framework, supervisor) - about a minute
REM     run_tests.bat --sim    + every measurement plan against the simulator; start_simulator.bat must
REM                            be running (the plans use the simulated IOCs and data server)
REM
REM Virtual environment: .venv-dev, created / updated automatically.
cd /d "%~dp0"
call tools\ensure_venv.bat .venv-dev dev requirements-ioc-host.txt requirements-control.txt requirements-dev.txt || goto fail
"%FASTQ_PY%" -m pytest -q tests\test_framework.py tests\test_supervisor.py
if errorlevel 1 goto fail
if /i "%~1"=="--sim" (
    "%FASTQ_PY%" -u tests\run_sim_plans.py
    if errorlevel 1 goto fail
)
echo.
echo All tests passed.
exit /b 0

:fail
echo.
echo Tests FAILED - see above.
if "%FASTQ_NO_PAUSE%"=="" pause
exit /b 1
