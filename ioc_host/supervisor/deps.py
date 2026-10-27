"""Check/install this repo's Python dependencies from requirements-ioc-host.txt.
Streams pip's output line-by-line via a callback so the GUI can show
progress instead of freezing for the duration of the install.
"""
from __future__ import annotations

import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS_PATH = REPO_ROOT / "requirements-ioc-host.txt"

_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


def _requirement_names(requirements_path: Path = REQUIREMENTS_PATH) -> list[str]:
    if not requirements_path.exists():
        return []
    names = []
    for line in requirements_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _NAME_RE.match(line)
        if match:
            names.append(match.group(1))
    return names


def missing_packages(requirements_path: Path = REQUIREMENTS_PATH) -> list[str]:
    missing = []
    for name in _requirement_names(requirements_path):
        try:
            metadata.version(name)
        except metadata.PackageNotFoundError:
            missing.append(name)
    return missing


_EXTERNALLY_MANAGED_MARKER = "externally-managed-environment"


def _run_pip(cmd: list[str], on_output: Callable[[str], None]) -> tuple[int, bool]:
    """Runs one pip command, streaming its output via on_output(). Returns
    (exit_code, saw_externally_managed_error)."""
    on_output(f"$ {' '.join(cmd)}")
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    saw_marker = False
    for line in process.stdout:
        line = line.rstrip("\n")
        if _EXTERNALLY_MANAGED_MARKER in line:
            saw_marker = True
        on_output(line)
    process.wait()
    on_output(f"$ pip exited with code {process.returncode}")
    return process.returncode, saw_marker


def install_requirements(
    on_output: Callable[[str], None],
    requirements_path: Path = REQUIREMENTS_PATH,
    python_executable: str | None = None,
) -> int:
    r"""Runs `pip install -r requirements-ioc-host.txt`, calling on_output(line) for
    every line of combined stdout/stderr as it arrives. Returns the
    process's exit code (0 = success).

    Normally this runs inside the dedicated .venv that start_ioc_host.bat
    creates, so PEP 668's "externally-managed-environment" guard never
    applies - a venv's pip is always allowed to install into it. This is
    only a safety net for the case where someone runs
    `python ioc_host\supervisor\supervisor.py` directly with a non-venv interpreter
    (e.g. a uv-managed one) that refuses a plain `pip install` for that
    reason: on seeing that specific error, this retries once with
    `--break-system-packages`, pip's own documented escape hatch, rather
    than leaving the user to re-run the command by hand."""
    python_exe = python_executable or sys.executable
    base_cmd = [python_exe, "-m", "pip", "install", "-r", str(requirements_path)]

    code, saw_marker = _run_pip(base_cmd, on_output)
    if code == 0 or not saw_marker:
        return code

    on_output(
        "Detected an externally-managed-environment error (PEP 668) - this looks like a "
        "dedicated interpreter (e.g. uv-managed), not a shared system Python, so retrying "
        "automatically with --break-system-packages..."
    )
    retry_cmd = base_cmd + ["--break-system-packages"]
    code, _ = _run_pip(retry_cmd, on_output)
    return code
