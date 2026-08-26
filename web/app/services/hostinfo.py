"""What the host actually looks like: NIC names and configured IPv4 addresses.

Read from the host's /proc (mounted read-only at HOST_PROC by
docker-compose.yml), and specifically through PID 1: /proc/net is a symlink to
self/net and follows the reading task's network namespace, so the web
container's own net/ lists its veth rather than the host's NICs. PID 1 in the
host's /proc is the host init, which lives in the root netns. Same reason
metrics._net_dev_path goes through PID 1.

Used by services.restore to decide whether a backup's server_ip and
boot_interface describe *this* machine before restoring them.

Both lookups return an empty set when the host /proc isn't mounted or isn't
readable. Callers must treat empty as "cannot verify" and fall back to trusting
the value -- never as "nothing matched", which would reject every value on a
host whose /proc simply isn't visible.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Host /proc if mounted by compose, else this container's own.
PROC = Path(os.environ.get("HOST_PROC", "/host/proc"))
if not PROC.exists():
    PROC = Path("/proc")

# A routing-table leaf in fib_trie: `   |-- 10.100.10.131`.
_TRIE_LEAF = re.compile(r"\|--\s+(\d{1,3}(?:\.\d{1,3}){3})\s*$")


def _host_net(name: str) -> str:
    """Contents of the host's net/<name>, or "" if it can't be read."""
    for candidate in (PROC / "1" / "net" / name, PROC / "net" / name):
        try:
            return candidate.read_text()
        except OSError:
            continue
    return ""


def interfaces() -> set[str]:
    """Every network interface name on the host, loopback excluded.

    Virtual interfaces (veth, docker0, bridges) are deliberately kept: they are
    real names that dnsmasq can bind to, and an admin who deliberately points
    Beacon at a bridge should not have that rejected as nonexistent.
    """
    names: set[str] = set()
    # Two header lines, then `  eth0: <counters>`.
    for line in _host_net("dev").splitlines()[2:]:
        name, sep, _ = line.partition(":")
        if not sep:
            continue
        name = name.strip()
        if name and name != "lo":
            names.add(name)
    return names


def local_ipv4s() -> set[str]:
    """Every IPv4 address assigned to this host.

    fib_trie prints each routing-table leaf as `|-- <addr>` followed by indented
    lines describing it. The ones tagged `host LOCAL` are addresses the machine
    answers to; the rest are network, broadcast and gateway entries, which would
    be wrong answers to "is this our IP?".
    """
    addrs: set[str] = set()
    leaf = ""
    for line in _host_net("fib_trie").splitlines():
        match = _TRIE_LEAF.search(line)
        if match:
            leaf = match.group(1)
            continue
        if leaf and "host LOCAL" in line:
            addrs.add(leaf)
            leaf = ""
    return addrs
