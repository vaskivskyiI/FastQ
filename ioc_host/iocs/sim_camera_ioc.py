"""IOC: simulated spectrometer camera (for tests and the simulator stack).

    python ioc_host/iocs/sim_camera_ioc.py --prefix TEST:CAM1: [--width 2048 --height 8]

Same PVs as every camera (ioc_host/common/camera.py); --mock is implied.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.camera import Camera, SimCamera  # noqa: E402
from ioc_host.common.devkit import run_instrument  # noqa: E402


class SimulatedCamera(Camera):
    TYPE = "sim_camera"
    DESCRIPTION = "Simulated spectrometer camera (HHG-like peaks + noise)"
    DEFAULT_PREFIX = "TEST:CAM1:"

    @classmethod
    def add_arguments(cls, parser) -> None:
        super().add_arguments(parser)
        parser.add_argument("--width", type=int, default=2048)
        parser.add_argument("--height", type=int, default=8)

    def open(self) -> None:
        self.drv = SimCamera(self.args.width, self.args.height)


if __name__ == "__main__":
    run_instrument(SimulatedCamera)
