"""Shared scaffolding for every IOC in ioc_host/iocs/.

Conventions (standard EPICS / areaDetector style, used by all IOCs):
  * <prefix><Name>      setpoint (writable)       e.g. FASTQ:ITC1:Setpoint
  * <prefix><Name>_RBV  its readback (read-only)  e.g. FASTQ:ITC1:Setpoint_RBV
  * plain read-only values have no suffix          e.g. FASTQ:ITC1:Temp
  * units (EGU) and precision (PREC) set on every analog PV
  * <prefix>IOC:...     iocStats PVs (HEARTBEAT, UPTIME, STARTTOD, SYSRESET, ...)
                        via caproto's StatusHelper - same names as devIocStats
  * <prefix>IOC:TYPE    instrument type (itc503, mfli, ...); clients check it against the
                        directory and build the device from <prefix>IOC:SCHEMA
  * <prefix>Connected   1 once the instrument answered
Nothing is written to an instrument at startup - only in response to an
explicit CA put to a setpoint PV.
"""
from __future__ import annotations

import argparse
import sys
import logging
from pathlib import Path

from caproto import ChannelType
from caproto.server import PVGroup, SubGroup, pvproperty, run, template_arg_parser
from caproto.server.stats import StatusHelper

from ioc_host.common import ca_windows
from ioc_host.common.access_control import apply_access_control

DEFAULT_SECURITY_CONFIG = Path(__file__).resolve().parents[1] / "security.yaml"

if sys.platform == "win32":  # iocStats FD_CNT: psutil has num_fds() only on Unix
    try:
        import psutil

        if not hasattr(psutil.Process, "num_fds"):
            psutil.Process.num_fds = psutil.Process.num_handles
    except ImportError:
        pass


def pv_bool(value) -> bool:
    """A caproto dtype=bool PV is an ENUM: its value arrives as the STRING
    "Off"/"On", and bool("Off") is True. Always decode through this."""
    if isinstance(value, bytes):
        value = value.decode()
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    return bool(value)


def enum_choice(value, choices: tuple) -> str:
    """ENUM put value -> label; clients send either the label or the index."""
    if isinstance(value, bytes):
        value = value.decode()
    if isinstance(value, str) and value in choices:
        return value
    return choices[int(value)]


class IOCBase(PVGroup):
    IOC_TYPE = "generic"

    stats = SubGroup(StatusHelper, prefix="IOC:")
    ioc_type = pvproperty(name="IOC:TYPE", value="", dtype=ChannelType.STRING, read_only=True)
    Connected = pvproperty(value=0, dtype=bool, read_only=True)

    @ioc_type.startup
    async def ioc_type(self, instance, async_lib):
        await instance.write(self.IOC_TYPE)


def ioc_arg_parser(default_prefix: str, desc: str) -> tuple[argparse.ArgumentParser, callable]:
    parser, split_args = template_arg_parser(default_prefix=default_prefix, desc=desc)
    parser.add_argument("--security-config", default=str(DEFAULT_SECURITY_CONFIG),
                        help="access-security file (see ioc_host/security.yaml.example); missing = no restrictions")
    return parser, split_args


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


def serve(ioc: PVGroup, args, run_options: dict) -> None:
    apply_access_control(ioc.pvdb, args.security_config)
    ca_windows.apply()  # several IOCs on one Windows host - see ca_windows.py
    run(ioc.pvdb, **run_options)
