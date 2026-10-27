"""Write protection of instrument controls (dashboard, AI tools).

Every writable param / action declares a safety level in its IOC schema (devkit `safety=`):
  safe       written at once
  confirm    the dashboard asks first
  protected  needs the lab PIN, checked here on the server (never only in the browser)
lab.yaml can override levels:  security: {pin_sha256: <hash>, overrides: {"laser1.ShutterOpen": protected}}
The PIN can also come from the env var FASTQ_CONTROL_PIN_SHA256. Hash a PIN with

    python -m fastq.security 1234

Five wrong PINs lock protected writes for LOCKOUT_S.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
import threading
import time

LEVELS = ("safe", "confirm", "protected")
LOCKOUT_AFTER, LOCKOUT_S = 5, 60.0
_fails: list[float] = []
_lock = threading.Lock()


def hash_pin(pin: str) -> str:
    return hashlib.sha256(("fastq:" + str(pin)).encode()).hexdigest()


def pin_hash(lab: dict) -> str | None:
    return os.environ.get("FASTQ_CONTROL_PIN_SHA256") or (lab.get("security") or {}).get("pin_sha256")


def check_pin(lab: dict, pin: str | None) -> None:
    """Raises PermissionError unless pin is the lab PIN."""
    expected = pin_hash(lab)
    if not expected:
        raise PermissionError("protected control: no PIN configured (lab.yaml security.pin_sha256)")
    with _lock:
        now = time.monotonic()
        _fails[:] = [t for t in _fails if now - t < LOCKOUT_S]
        if len(_fails) >= LOCKOUT_AFTER:
            raise PermissionError(f"too many wrong PINs - protected controls locked for {LOCKOUT_S:.0f} s")
        if not pin or not hmac.compare_digest(hash_pin(pin), expected):
            _fails.append(now)
            raise PermissionError("wrong PIN" if pin else "protected control: PIN required")
        _fails.clear()


def level(lab: dict, device: str, name: str, declared: str | None) -> str:
    """Effective safety of a control: lab override, else what the IOC declares, else confirm."""
    over = ((lab.get("security") or {}).get("overrides") or {}).get(f"{device}.{name}")
    lvl = over or declared or "confirm"
    return lvl if lvl in LEVELS else "protected"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m fastq.security <PIN>   (prints the line for lab.yaml)")
    print(f"security:\n  pin_sha256: {hash_pin(sys.argv[1])}")
