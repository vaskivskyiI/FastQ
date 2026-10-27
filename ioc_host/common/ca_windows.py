"""Windows fix for running several caproto IOCs on ONE machine.

caproto binds each IOC's TCP listener with SO_REUSEADDR. On Windows that
option lets a second process bind the SAME TCP port (5064) without error,
so every IOC on the host "listens" on 5064 but all client TCP connections
reach only one of them. Symptom (seen 2026-09-26 with carbide + dummy IOC
running together): the UDP name search IS answered, then "Timeout while
awaiting channel creation" / monitor panel shows no data for every IOC
but one.

Fix: on Windows, bind TCP with SO_EXCLUSIVEADDRUSE instead. The second
IOC's bind to 5064 then fails, and caproto's own fallback
(`_bind_tcp_sockets_with_consistent_port_number` -> random free port)
kicks in; that port is advertised in search replies, so clients need no
configuration. UDP 5064 is unchanged (still shared, as it must be).

Call `apply()` once, before `caproto.server.run(...)`.
"""
from __future__ import annotations

import socket
import sys


async def _create_exclusive_tcp_socket(addr, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    sock.setblocking(False)
    try:
        sock.bind((addr, port))
    except OSError:
        sock.close()
        raise
    return sock


def apply() -> None:
    if sys.platform != "win32":
        return
    import caproto.asyncio.server as ca_asyncio_server

    ca_asyncio_server._create_bound_tcp_socket = _create_exclusive_tcp_socket
