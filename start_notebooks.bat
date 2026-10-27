@echo off
REM ANALYSIS: JupyterLab with the notebooks of notebooks\ (4DMAG pump-probe, transport R(T) / I-V,
REM run overview). Reads runs from the data server of the lab file:
REM
REM     start_notebooks.bat                        this PC's control\lab.yaml (made from an example if missing)
REM     start_notebooks.bat control\lab_sim.yaml   the simulator's runs
REM
REM Virtual environment: .venv-analysis (control + analysis requirements), created / updated
REM automatically. With dashboard accounts set FASTQ_USER / FASTQ_PASSWORD (a viewer account).
cd /d "%~dp0"
call tools\ensure_venv.bat .venv-analysis analysis requirements-ioc-host.txt requirements-control.txt requirements-analysis.txt || goto fail
set "FASTQ_LAB=%~1"
if "%FASTQ_LAB%"=="" set "FASTQ_LAB=control\lab.yaml"
"%FASTQ_PY%" tools\local_config.py lab "%FASTQ_LAB%"
if errorlevel 2 goto edit
if errorlevel 1 goto fail
set "FASTQ_LAB=%CD%\%FASTQ_LAB%"
"%FASTQ_PY%" -m jupyter lab notebooks
if errorlevel 1 goto fail
exit /b 0

:edit
echo.
pause
exit /b 1

:fail
echo.
echo start_notebooks failed - see the messages above.
pause
exit /b 1
