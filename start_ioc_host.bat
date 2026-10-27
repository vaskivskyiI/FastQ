@echo off
REM IOC HOST (the PC wired to the instruments): the supervisor that runs / restarts its IOCs.
REM
REM     start_ioc_host.bat                     GUI, this PC's ioc_host\supervisor_config.yaml
REM     start_ioc_host.bat "" --headless       no window (as the installed service)
REM     start_ioc_host.bat <host file>         another host file
REM
REM ioc_host\supervisor_config.yaml is this PC's own file (not in git, a git pull never touches it). On the
REM first start it is copied from an example of ioc_host\hosts\*.example.yaml - then edit it.
REM Virtual environment: .venv (requirements-ioc-host.txt), created / updated automatically.
REM To start it with Windows instead: deploy\install_ioc_host.ps1 (deploy\README.md).
cd /d "%~dp0"
call tools\ensure_venv.bat .venv ioc-host requirements-ioc-host.txt || goto fail
set "HOSTFILE=%~1"
if "%HOSTFILE%"=="" set "HOSTFILE=ioc_host\supervisor_config.yaml"
"%FASTQ_PY%" tools\local_config.py host "%HOSTFILE%"
if errorlevel 2 goto edit
if errorlevel 1 goto fail
set "FASTQ_IOC_CONFIG=%CD%\%HOSTFILE%"
if /i "%~2"=="--headless" (
    "%FASTQ_PY%" ioc_host\supervisor\supervisor.py --headless --config "%HOSTFILE%"
) else (
    "%FASTQ_PY%" ioc_host\supervisor\supervisor.py --config "%HOSTFILE%"
)
if errorlevel 1 goto fail
exit /b 0

:edit
echo.
pause
exit /b 1

:fail
echo.
echo start_ioc_host failed - see the messages above.
pause
exit /b 1
