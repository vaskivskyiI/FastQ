"""Plans offered by the queue server = the functions in control/experiments/*.py and in the private
experiment folders (experiment_dirs(): plans and recipes that must not be in the public repository).

load_plans(lab) imports every experiments file (not starting with _) and returns
{name: plan}: a file's PLANS list if it has one, else every public generator function
defined in it. lab.yaml `plans: {exclude: [...]}` removes names. A broken file is
skipped with an error message, so one mistake never takes all plans down.

Every plan is wrapped so that its runs record how it was called: start
document "plan_request" = {name, kwargs: EVERY parameter incl. defaults
(devices by name), args}, "roles" = which device played which lab role, and
"code" = {version of the code archive (fastq.provenance), plan file, source}.
With that a run can be repeated exactly, even after the plan was changed or
removed (dashboard: "run again" / restore from the archive).
"""
from __future__ import annotations

import functools
import importlib.util
import inspect
import logging
import os
import sys
import traceback
from pathlib import Path

log = logging.getLogger("fastq.plans")
EXPERIMENTS = Path(__file__).resolve().parents[1] / "experiments"


def experiment_dirs() -> list[Path]:
    """control/experiments, then the private ones (same layout: *.py plans, presets.yaml recipes):
    FASTQ_EXPERIMENTS=<dir>[;<dir>...] and ../FastQ-private/control/experiments next to the repository.
    Private files are archived and recorded as experiments/<file> like the public ones."""
    extra = [Path(d) for d in os.environ.get("FASTQ_EXPERIMENTS", "").split(os.pathsep) if d]
    extra.append(EXPERIMENTS.parents[2] / "FastQ-private" / "control" / "experiments")
    dirs = [EXPERIMENTS]
    for d in extra:
        if d.is_dir() and d.resolve() not in [x.resolve() for x in dirs]:
            dirs.append(d)
    return dirs
ERRORS: dict[str, str] = {}
REGISTRY: dict = {}  # the plans of the last load_plans (fastq.plans.flow.sequence runs them by name)


def _jsonable(value):
    """Plan argument -> JSON-safe value (devices / signals by name)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, np.ndarray):
            return value.tolist()
    except ImportError:
        pass
    return repr(value)


def recorded(func, code: dict | None = None):
    """Same plan (signature, docstring), plus md plan_request / roles / code."""
    from bluesky.preprocessors import msg_mutator

    sig = inspect.signature(func)
    try:
        src = Path(inspect.getsourcefile(func)).resolve()
        plan_file = (src.relative_to(EXPERIMENTS.parent).as_posix() if src.is_relative_to(EXPERIMENTS.parent)
                     else f"experiments/{src.name}")  # a private experiments folder (archived like that too)
    except TypeError:
        plan_file = getattr(func, "__module__", "")
    code = {**(code or {}), "plan_file": plan_file, "function": func.__name__}

    own_comment = "comment" in sig.parameters

    @functools.wraps(func)
    def plan(*args, **kwargs):
        comment = "" if own_comment else str(kwargs.pop("comment", "") or "")
        try:
            bound = sig.bind_partial(*args, **kwargs)
            bound.apply_defaults()           # record every parameter, defaults included
            call = {k: _jsonable(v) for k, v in bound.arguments.items()}
            for p in sig.parameters.values():  # flatten **devices style keyword catch-alls
                if p.kind is p.VAR_KEYWORD and p.name in call:
                    call.update(call.pop(p.name))
                elif p.kind is p.VAR_POSITIONAL and p.name in call:
                    call["args"] = call.pop(p.name)
        except TypeError:
            call = {"args": _jsonable(args), **{k: _jsonable(v) for k, v in kwargs.items()}}
        from fastq.session import _ROLES

        md = {"plan_request": {"name": func.__name__, "kwargs": call},
              "roles": {r: getattr(d, "name", str(d)) for r, d in _ROLES.items()}, "code": code}
        if comment:
            md["comment"] = comment  # the run description (ELOG text / subject, like 4dmag's run info)
        md["plan_name"] = func.__name__  # the plan that was queued, not the building block it calls

        def stamp(msg):
            if msg.command != "open_run":
                return msg
            if msg.kwargs.get("queued_plan"):  # a recorded plan inside this one (a sequence step): its own
                return msg._replace(kwargs={"part_of": func.__name__, **msg.kwargs})  # name / request stay
            # the queued plan's identity wins over a building block's ("count", "scan"); every run has a
            # run_kind: the plan's own, else the queued plan's name
            kw = {**msg.kwargs, **md, "queued_plan": func.__name__}
            kw.setdefault("run_kind", None)
            kw["run_kind"] = kw["run_kind"] or func.__name__
            return msg._replace(kwargs=kw)
        return (yield from msg_mutator(func(*args, **kwargs), stamp))

    if not own_comment:  # every plan takes comment="..." (shown in the queue form, stored with the run)
        params = list(sig.parameters.values())
        extra = inspect.Parameter("comment", inspect.Parameter.KEYWORD_ONLY, default="", annotation=str)
        pos = next((i for i, p in enumerate(params) if p.kind is p.VAR_KEYWORD), len(params))
        plan.__signature__ = sig.replace(parameters=[*params[:pos], extra, *params[pos:]])
    return plan


def _module_plans(module) -> list:
    if hasattr(module, "PLANS"):
        return list(module.PLANS)
    return [obj for name, obj in vars(module).items()
            if not name.startswith("_") and inspect.isgeneratorfunction(obj) and obj.__module__ == module.__name__]


def load_plans(lab: dict | None = None, directories: list[Path] | None = None) -> dict:
    exclude = set(((lab or {}).get("plans") or {}).get("exclude") or [])
    plans: dict = {}
    code = None
    try:
        from fastq.provenance import archive_code

        code = archive_code(lab or {})
    except Exception:  # noqa: BLE001 - never block the plans because of the archive
        log.exception("code archive not written")
    ERRORS.clear()
    restored = []
    try:  # plans restored from the code archive (fastq.provenance.restore_plan)
        from fastq.session import data_root

        restored = sorted((data_root(lab) / "restored").glob("restored_*.py")) if lab else []
    except Exception:  # noqa: BLE001
        pass
    directories = directories or experiment_dirs()
    for path in [*(p for d in directories for p in sorted(d.glob("*.py"))), *restored]:
        if path.name.startswith("_"):
            continue
        mod_name = f"experiments.{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001
            ERRORS[path.name] = traceback.format_exc(limit=3)
            log.error("experiments/%s not loaded:\n%s", path.name, ERRORS[path.name])
            continue
        for plan in _module_plans(module):
            name = plan.__name__
            if name in exclude:
                continue
            if name in plans and plans[name].__wrapped__ is not plan:
                log.error("plan %s in %s already defined in another file - skipped", name, path.name)
                continue
            plans[name] = recorded(plan, code)
    log.info("plans: %d from %s%s", len(plans), ", ".join(map(str, directories)),
             f" (excluded {sorted(exclude)})" if exclude else "")
    REGISTRY.clear()
    REGISTRY.update(plans)
    return plans
