"""Ordering of multi-dimensional scan conditions.

    dimensions  {name: [values]}   single value = held fixed
    nesting     names looped as nested loops, outermost first
    randomized  names whose combined cartesian product is shuffled as one
                block, re-shuffled each time the nested dims advance
Any dimension in neither list is appended to the nesting order.
"""
from __future__ import annotations

import itertools
import random


def build_conditions(dimensions: dict[str, list], randomized=(), nesting=(), seed=None) -> list[dict]:
    names = list(dimensions)
    randomized, nesting = list(randomized or []), list(nesting or [])
    unknown = (set(randomized) | set(nesting)) - set(names)
    if unknown:
        raise ValueError(f"unknown dimension(s): {sorted(unknown)}")
    if set(randomized) & set(nesting):
        raise ValueError("a dimension cannot be both randomized and nested")
    for name, values in dimensions.items():
        if not isinstance(values, (list, tuple)) or not values:
            raise ValueError(f"dimension {name!r} must be a non-empty list")
    nesting += [n for n in names if n not in randomized and n not in nesting]
    rand = sorted(randomized)
    rng = random.Random(seed)

    def recurse(i):
        if i == len(nesting):
            combos = list(itertools.product(*[dimensions[n] for n in rand])) if rand else [()]
            rng.shuffle(combos)
            for combo in combos:
                yield dict(zip(rand, combo))
            return
        for value in dimensions[nesting[i]]:
            for rest in recurse(i + 1):
                yield {nesting[i]: value, **rest}

    return list(recurse(0))


def substitute(template, values: dict):
    """Replace "$name" strings (recursively in dicts/lists) by values[name]."""
    if isinstance(template, dict):
        return {k: substitute(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [substitute(v, values) for v in template]
    if isinstance(template, str) and template.startswith("$"):
        key = template[1:]
        if key not in values:
            raise KeyError(f"{template} is not a scan dimension ({sorted(values)})")
        return values[key]
    return template
