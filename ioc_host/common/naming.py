"""PV prefixes and device names - one rule for the directory IOC, the supervisor and the clients.

    prefixes("--camera-index 1 --prefix FDM:CAMMAIN: --also '--camera-index 2 --prefix FDM:CAMREF:'")
        -> ["FDM:CAMMAIN:", "FDM:CAMREF:"]       (devkit --also serves more instruments per process)
    device_name("FDM:CAMMAIN:") -> "cammain"
"""
from __future__ import annotations

import shlex


def _split(args: str) -> list[str]:
    try:
        return shlex.split(args or "")
    except ValueError:
        return shlex.split(args or "", posix=False)


def prefixes(args: str) -> list[str]:
    """Every --prefix of an IOC command line, including those inside --also "<options>"."""
    out, tokens = [], _split(args)
    for i, t in enumerate(tokens):
        if t == "--prefix" and i + 1 < len(tokens):
            out.append(tokens[i + 1].strip("'\""))
        elif t.startswith("--prefix="):
            out.append(t.split("=", 1)[1].strip("'\""))
        elif t == "--also" and i + 1 < len(tokens):
            out += prefixes(tokens[i + 1])
        elif t.startswith("--also="):
            out += prefixes(t.split("=", 1)[1])
    return out


def device_name(prefix: str) -> str:
    return prefix.rstrip(":").split(":")[-1].lower()
