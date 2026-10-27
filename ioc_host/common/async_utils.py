"""Blocking driver calls from asyncio IOCs.

run_blocking() runs a blocking call (GPIB, LabOne, HTTP) in a worker thread so
the IOC's event loop stays responsive. The pool has several threads: an IOC
where several scans/putters share one instrument must pass one asyncio.Lock
per instrument as lock= (interleaved GPIB queries desynchronize replies).
"""
from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable, TypeVar

T = TypeVar("T")


async def run_blocking(
    func: Callable[..., T], *args: Any, lock: "asyncio.Lock | None" = None, **kwargs: Any
) -> T:
    loop = asyncio.get_running_loop()
    call = functools.partial(func, *args, **kwargs)
    if lock is None:
        return await loop.run_in_executor(None, call)
    async with lock:
        return await loop.run_in_executor(None, call)


def safe_default(value: Any, default: float = float("nan")) -> float:
    """Coerce a possibly-None/str reading into a float PV-safe value."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
