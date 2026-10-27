"""IOC: device directory - the one well-known PV name clients start from.

    python ioc_host/iocs/directory_ioc.py --prefix FASTQ:DIR:

Publishes, from ioc_host/supervisor_config.yaml (re-read every few seconds):
  FASTQ:DIR:Devices     JSON list: [{"name", "type", "prefix", "enabled", "host"}, ...] - this host
  FASTQ:DIR:AllDevices  same, merged with the Devices of every --peers directory
  FASTQ:DIR:Count       number of enabled devices (this host)
  FASTQ:DIR:Host        host name of the IOC host
  FASTQ:DIR:Peers       peers and whether each answered

Several IOC hosts: each runs its own directory with a unique prefix and
lists the others as peers (symmetric), e.g.
  host A:  directory_ioc.py --prefix FASTQ:DIR:      --peers FASTQ:DIR:B:
  host B:  directory_ioc.py --prefix FASTQ:DIR:B:    --peers FASTQ:DIR:
Any directory's AllDevices is then the whole lab; clients still need one
name (FASTQ:DIR:), and may list several in FASTQ_DIRECTORY for redundancy.
Device PV prefixes must be unique across all hosts.

How discovery works (all standard Channel Access, no IP addresses anywhere):
  1. a client resolves FASTQ:DIR:Devices by CA name search (UDP broadcast on
     the LAN, or EPICS_CA_ADDR_LIST for other subnets),
  2. reads the device list, and for each device checks <prefix>IOC:TYPE,
  3. builds the ophyd device from <prefix>IOC:SCHEMA (control/fastq/generic.py).
Each entry: name (from the prefix, ioc_host/common/naming.py), process (the supervisor key), type, prefix.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml  # noqa: E402
from caproto import ChannelType  # noqa: E402
from caproto.server import pvproperty  # noqa: E402

from ioc_host.common.async_utils import run_blocking  # noqa: E402
from ioc_host.common.naming import device_name, prefixes  # noqa: E402
from ioc_host.common.ioc_base import IOCBase, ioc_arg_parser, serve, setup_logging  # noqa: E402

CONFIG = Path(os.environ.get("FASTQ_IOC_CONFIG") or Path(__file__).resolve().parents[1] / "supervisor_config.yaml")


def read_directory(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = []
    for name, dev in sorted((data.get("devices") or {}).items()):
        script = dev["ioc_script"]
        if script == "directory_ioc":
            continue
        for prefix in prefixes(dev.get("args") or "") or [""]:  # one entry per instrument of the process
            out.append({"name": device_name(prefix) if prefix else name, "process": name,
                        "type": script.removesuffix("_ioc"), "prefix": prefix, "enabled": bool(dev.get("enabled"))})
    return out


class DirectoryIOC(IOCBase):
    IOC_TYPE = "directory"

    Devices = pvproperty(value="[]", dtype=str, max_length=8192, read_only=True)
    AllDevices = pvproperty(value="[]", dtype=str, max_length=32768, read_only=True)
    Peers = pvproperty(value="", dtype=str, max_length=1024, read_only=True)
    Count = pvproperty(value=0, dtype=int, read_only=True)
    Host = pvproperty(value="", dtype=ChannelType.STRING, read_only=True)

    def __init__(self, config: Path, peers: list[str], *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.config = config
        self.peers = peers
        self._ctx = None

    def _read_peers(self) -> tuple[list[dict], list[str]]:
        """Devices of every peer directory (blocking CA reads, run in a thread)."""
        if not self.peers:
            return [], []
        from caproto.threading.client import Context

        self._ctx = self._ctx or Context()
        merged, status = [], []
        for prefix in self.peers:
            try:
                (pv,) = self._ctx.get_pvs(prefix + "Devices", timeout=2)
                data = pv.read(timeout=2).data
                merged += json.loads(bytes(bytearray(data)).split(bytes(1), 1)[0].decode())
                status.append(f"{prefix} ok")
            except Exception as exc:  # a peer being down must not break this directory
                status.append(f"{prefix} DOWN ({type(exc).__name__})")
        return merged, status

    @Devices.scan(period=5.0)
    async def Devices(self, instance, async_lib):
        try:
            devices = read_directory(self.config)
        except Exception:
            await self.Connected.write(0)
            raise
        host = socket.gethostname()
        for d in devices:
            d["host"] = host
        text = json.dumps(devices)
        if text != instance.value:
            await instance.write(text)
        peer_devices, status = await run_blocking(self._read_peers)
        seen = {d["prefix"] for d in devices}
        merged = devices + [d for d in peer_devices if d["prefix"] not in seen]
        all_text = json.dumps(merged)
        if all_text != self.AllDevices.value:
            await self.AllDevices.write(all_text)
        await self.Peers.write("; ".join(status)[:1000])
        await self.Count.write(sum(d["enabled"] for d in devices))
        await self.Host.write(socket.gethostname()[:39])
        await self.Connected.write(1)


def main() -> None:
    parser, split_args = ioc_arg_parser("FASTQ:DIR:", "device directory IOC")
    parser.add_argument("--config", default=str(CONFIG))
    parser.add_argument("--peers", default="", help="comma-separated prefixes of the other hosts' directories")
    args = parser.parse_args()
    ioc_options, run_options = split_args(args)
    setup_logging()
    peers = [p.strip() for p in args.peers.split(",") if p.strip()]
    serve(DirectoryIOC(Path(args.config), peers, **ioc_options), args, run_options)


if __name__ == "__main__":
    main()
