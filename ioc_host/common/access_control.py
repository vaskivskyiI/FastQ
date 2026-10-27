"""Per-PV read/write access control for the caproto IOCs in this stack.

Built on caproto's own, native mechanism: every `ChannelData` instance
(every PV) has a `check_access(hostname, username)` method the server calls
on every read and every write; it returns some combination of
`AccessRights.READ` / `AccessRights.WRITE`, and caproto raises `Forbidden`
itself if the requested operation isn't in the returned set - verified by
reading caproto 1.3.0's source (`caproto/_data.py`: `auth_read`/`auth_write`
call `self.check_access(...)` and reject the operation if the right isn't
granted). This module loads a YAML config, matches each PV's full name
against glob patterns, and installs a per-instance `check_access` override
implementing an allowlist.

`hostname` and `username` come from the CA protocol's connection handshake:
every Channel Access client (caproto, pyepics/libca, CS-Studio, ...) sends
its OS hostname and `getpass.getuser()` username automatically when it
connects - this isn't something a client opts into, so any CA client that
reaches an IOC's port will present *some* hostname/username. But neither is
authenticated or encrypted - they're plain strings the client asserts, and
anyone who can run code on the network could send whatever they want. This
protects against accidents and unauthorized software connecting from
somewhere else in the lab, not against a hostile actor - genuinely
sensitive controls (the laser shutter, say) should also be restricted at
the OS/network firewall level, or kept behind a physical interlock, not
rely on this alone.

security.yaml format:

    default_read_hosts: ["*"]     # PVs with no matching rule: readable by anyone
    default_write_hosts: ["*"]    # PVs with no matching rule: writable by anyone
    rules:
      - pattern: "FASTQ:LASER1:ShutterOpenSet"
        write_hosts: ["control-pc"]        # only this hostname may write
      - pattern: "FASTQ:LASER1:*"
        write_hosts: ["control-pc"]
      - pattern: "FASTQ:KEITHLEY1:*"
        read_hosts: ["control-pc", "monitor-pc"]
        write_hosts: ["control-pc"]

`pattern` is matched against the PV's full name (prefix + suffix, e.g.
"FASTQ:LASER1:ShutterOpenSet") using shell-glob syntax (`fnmatch`). The first
matching rule wins; a PV matching no rule falls back to
`default_read_hosts`/`default_write_hosts`. Host/user comparisons are
case-insensitive; `"*"` in a list means "anyone". A rule can omit
`read_hosts` or `write_hosts` to leave that right at its default for PVs it
matches.

If security.yaml doesn't exist, access control is not applied at all -
every PV is fully open, exactly like before this feature existed. That's a
deliberate default so an IOC never silently starts rejecting operators
because a config file didn't get copied somewhere.
"""
from __future__ import annotations

import fnmatch
import logging
import types
from pathlib import Path
from typing import Any

logger = logging.getLogger("access_control")


def load_security_config(path: Path | str) -> dict[str, Any] | None:
    path = Path(path)
    if not path.exists():
        logger.warning(
            "No security config at %s - every PV in this IOC is unrestricted "
            "(any host/user can read and write). See security.yaml.example.",
            path,
        )
        return None

    import yaml

    with open(path, "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    config.setdefault("default_read_hosts", ["*"])
    config.setdefault("default_write_hosts", ["*"])
    config.setdefault("rules", [])
    return config


def _host_allowed(hostname: str | None, allowlist: list[str]) -> bool:
    if not allowlist:
        return False
    hostname = (hostname or "").lower()
    for entry in allowlist:
        if entry == "*" or fnmatch.fnmatch(hostname, str(entry).lower()):
            return True
    return False


def _rule_for(pv_name: str, rules: list[dict]) -> dict | None:
    for rule in rules:
        if fnmatch.fnmatch(pv_name, str(rule.get("pattern", ""))):
            return rule
    return None


def _make_check_access(pv_name: str, read_hosts: list[str], write_hosts: list[str]):
    from caproto import AccessRights

    def check_access(self, hostname, username):  # noqa: ANN001 - caproto's own signature
        access = AccessRights.NO_ACCESS
        if _host_allowed(hostname, read_hosts):
            access |= AccessRights.READ
        if _host_allowed(hostname, write_hosts):
            access |= AccessRights.WRITE
        if access == AccessRights.NO_ACCESS:
            logger.warning(
                "ACCESS DENIED: %s from host=%r user=%r (read_hosts=%s write_hosts=%s)",
                pv_name, hostname, username, read_hosts, write_hosts,
            )
        return access

    return check_access


def apply_access_control(pvdb: dict[str, Any], config_path: Path | str) -> int:
    """Install a `check_access` override on every PV in `pvdb` per
    security.yaml. Returns the number of PVs that got a non-default rule
    (0 if the config file doesn't exist, or every PV falls back to the
    wide-open defaults)."""
    config = load_security_config(config_path)
    if config is None:
        return 0

    default_read = [str(h) for h in config["default_read_hosts"]]
    default_write = [str(h) for h in config["default_write_hosts"]]
    rules = config["rules"]

    restricted = 0
    for pv_name, channel_data in pvdb.items():
        rule = _rule_for(pv_name, rules)
        read_hosts = default_read
        write_hosts = default_write
        if rule is not None:
            read_hosts = [str(h) for h in rule.get("read_hosts", default_read)]
            write_hosts = [str(h) for h in rule.get("write_hosts", default_write)]
            restricted += 1
            logger.info(
                "Access rule applied to %s: read_hosts=%s write_hosts=%s",
                pv_name, read_hosts, write_hosts,
            )

        bound = _make_check_access(pv_name, read_hosts, write_hosts)
        channel_data.check_access = types.MethodType(bound, channel_data)

    logger.info(
        "Access control loaded from %s: %d/%d PVs have a non-default rule.",
        config_path, restricted, len(pvdb),
    )
    return restricted
