@echo off
REM Makes sure a virtual environment exists and has its requirements - used by the start_*.bat files.
REM
REM     call tools\ensure_venv.bat <venv folder> <set name> <requirements file> [more files ...]
REM
REM Creates <venv folder> (relative to the repository) if it is missing, with a Python 3.10-3.13
REM (FASTQ_PYTHON if set, else the py launcher 3.12 / 3.13 / 3.11 / 3.10, else "python" on PATH).
REM Installs the requirements when the venv is new or when one of the files changed since the
REM last install (a copy of them is kept in <venv>\fastq-<set name>.txt) - later starts are instant.
REM Returns errorlevel 1 on failure; on success FASTQ_PY = the venv's python.exe.
setlocal enabledelayedexpansion
cd /d "%~dp0.."
set "VENV=%~1"
set "SET_NAME=%~2"
shift
shift
set "PIPARGS="
set "FILES="
:args
if "%~1"=="" goto argsdone
if not exist "%~1" (
    echo [ensure_venv] requirements file %~1 not found
    exit /b 1
)
set "PIPARGS=!PIPARGS! -r "%~1""
set "FILES=!FILES! "%~1""
shift
goto args
:argsdone

set "VPY=%CD%\%VENV%\Scripts\python.exe"
if not exist "%VPY%" goto make_venv
REM a venv copied from another PC (or whose Python was uninstalled) does not start: keep it aside, make a new one
"%VPY%" -c "import sys" >nul 2>&1 && goto have_venv
set "ASIDE=%VENV%.broken-%RANDOM%"
echo [ensure_venv] %VENV% does not start ^(its Python is gone^) - moved to !ASIDE!, creating a new one
move "%VENV%" "!ASIDE!" >nul || (
    echo [ensure_venv] could not move %VENV% aside - delete it and start again
    exit /b 1
)

:make_venv
REM ---- find a Python to create the venv with
set "PY="
if defined FASTQ_PYTHON set "PY=%FASTQ_PYTHON%"
if not defined PY (
    for %%v in (3.12 3.13 3.11 3.10) do (
        if not defined PY (
            py -%%v -c "import sys" >nul 2>&1 && set "PY=py -%%v"
        )
    )
)
if not defined PY (
    python -c "import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 13) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [ensure_venv] No Python 3.10 - 3.13 found. Install Python 3.12 64-bit from python.org
    echo               ^(with the "py" launcher^), or set FASTQ_PYTHON to a python.exe.
    exit /b 1
)
echo [ensure_venv] creating %VENV% with !PY! ...
!PY! -m venv "%VENV%"
if errorlevel 1 (
    echo [ensure_venv] could not create %VENV% - run "!PY! -m venv %VENV%" yourself to see why
    exit /b 1
)
"%VPY%" -m pip install -q --upgrade pip >nul 2>&1

:have_venv
REM ---- install when the requirement files differ from the copy of the last install
set "STAMP=%VENV%\fastq-%SET_NAME%.txt"
if exist "%STAMP%.new" del "%STAMP%.new"
for %%f in (%FILES%) do type %%f >> "%STAMP%.new"
fc /b "%STAMP%.new" "%STAMP%" >nul 2>&1
if not errorlevel 1 (
    del "%STAMP%.new"
    goto done
)
echo [ensure_venv] installing the %SET_NAME% requirements into %VENV% (first start or changed files) ...
REM a venv made by uv has no pip: bootstrap it, or let uv install (it is then on PATH)
set "PIP="%VPY%" -m pip"
"%VPY%" -m pip --version >nul 2>&1 || "%VPY%" -m ensurepip --upgrade >nul 2>&1
"%VPY%" -m pip --version >nul 2>&1 || (
    uv --version >nul 2>&1 && set "PIP=uv pip" && set "PIPARGS= --python "%VPY%"!PIPARGS!"
)
!PIP! install!PIPARGS!
if errorlevel 1 (
    del "%STAMP%.new"
    echo [ensure_venv] pip install failed - see above ^(network? a package without a wheel for this Python?^)
    exit /b 1
)
move /y "%STAMP%.new" "%STAMP%" >nul

:done
endlocal & set "FASTQ_PY=%VPY%"
exit /b 0
