"""Wait until a device directory answers (start_simulator.bat starts the control host after it).

    python sim/wait_for_directory.py [TEST:DIR:] [timeout_s]      exit 0 = it answers, 1 = timeout
"""
from __future__ import annotations

import sys
import time

from caproto.threading.client import Context


def main() -> int:
    prefix = sys.argv[1] if len(sys.argv) > 1 else "TEST:DIR:"
    limit = float(sys.argv[2]) if len(sys.argv) > 2 else 90.0
    ctx, t0 = Context(), time.monotonic()
    while time.monotonic() - t0 < limit:
        try:
            pv, = ctx.get_pvs(prefix + "Devices", timeout=5)
            pv.read(timeout=5)
            return 0
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(2)
    return 1


if __name__ == "__main__":
    sys.exit(main())
