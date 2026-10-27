"""TEMPLATE - copy to a new file without the leading underscore (e.g. my_project.py).

Every public generator function defined in the file becomes a plan in the queue server
(names starting with _ are private helpers). The docstring is the description shown in
the dashboard; parameter defaults are shown as hints. Conventions: README.md here - devices
are parameters named after their lab role (None = the role's device, fastq.session.resolve),
quantities carry their unit. A plan that is only an existing plan with fixed parameters is a
recipe in presets.yaml, not a new function.
"""
from __future__ import annotations

from fastq.plans.scan import nd_scan
from fastq.session import resolve


def my_lockin_map(positions_mm: list = (0.0, 0.5, 1.0), repeats: int = 3, period_s: float = 0.0,
                  lockin=None, stage=None):
    """Lock-in X/Y at each stage position, `repeats` times (one run)."""
    lockin, stage = resolve(lockin, "lockin"), resolve(stage, "stage")
    yield from nd_scan([{"repeat": repeats}, {"device": stage, "values": list(positions_mm)}],
                       detectors=[lockin], period_s=period_s, md={"run_kind": "Lock-in map"})
