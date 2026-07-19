"""Host performance metrics read straight from /proc (no external deps).

The web container mounts the host's /proc read-only at HOST_PROC (see
docker-compose.yml) so these numbers reflect the whole machine -- including the
nginx and nfs services that actually push images to clients -- not just this
container. If the host /proc isn't mounted we fall back to the container's own
/proc, which still works but won't show image-push load from the sibling
containers.

Rates (CPU%, disk B/s, network B/s) are deltas between successive reads, so the
first call after startup returns zero rates; the dashboard polls on an interval
and gets live numbers from the second sample onward.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

# Host /proc if mounted by compose, else this container's own.
PROC = Path(os.environ.get("HOST_PROC", "/host/proc"))
if not PROC.exists():
    PROC = Path("/proc")

# Pseudo / virtual block devices we don't want to count as real disk I/O.
_SKIP_DISK_PREFIXES = ("loop", "ram", "dm-", "sr", "fd")
# Virtual / loopback network interfaces to ignore for throughput.
_SKIP_NET_PREFIXES = ("lo", "veth", "docker", "br-", "virbr")

_lock = threading.Lock()
# Previous sample, keyed so rate = (now - prev) / elapsed.
_prev: dict | None = None


def _read(name: str) -> str:
    return (PROC / name).read_text()


def _net_dev_path() -> Path:
    """Path to the *host's* net/dev, not this container's.

    /proc/net is a symlink to self/net, and a task's net/ directory follows its
    network namespace -- so reading HOST_PROC/net/dev from inside the container
    still reports the container's own veth, not the host NIC. That made the
    dashboard's network graph show only its own dashboard polling traffic while
    the disk graph (diskstats isn't namespaced) correctly showed image pushes.
    PID 1 in the host's /proc is the host init, which lives in the root netns,
    so its net/dev has the real interface counters. Falls back to the plain
    path if the host /proc isn't mounted or PID 1 isn't readable.
    """
    host_init = PROC / "1" / "net" / "dev"
    try:
        host_init.read_text()
        return host_init
    except OSError:
        return PROC / "net" / "dev"


def _cpu_totals() -> tuple[int, int]:
    """Return (idle_plus_iowait, total) jiffies from the aggregate cpu line."""
    line = _read("stat").splitlines()[0]          # "cpu  u n s idle iowait irq ..."
    parts = [int(x) for x in line.split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)  # idle + iowait
    return idle, sum(parts)


def _meminfo() -> dict[str, int]:
    out = {}
    for line in _read("meminfo").splitlines():
        key, _, rest = line.partition(":")
        out[key] = int(rest.split()[0]) * 1024     # kB -> bytes
    return out


def _disk_bytes() -> tuple[int, int]:
    """Sum (read_bytes, written_bytes) across whole block devices.

    Counts only whole disks, never their partitions -- diskstats lists both and
    the whole-disk row already totals the partitions, so counting partitions too
    would double the figures. A name is a partition if another listed device name
    is a prefix of it (sda1 of sda, nvme0n1p1 of nvme0n1).
    """
    rows = []
    for line in _read("diskstats").splitlines():
        f = line.split()
        if len(f) < 14:
            continue
        dev = f[2]
        if dev.startswith(_SKIP_DISK_PREFIXES):
            continue
        # Fields 6 and 10 (1-indexed) = sectors read / written.
        rows.append((dev, int(f[5]) * 512, int(f[9]) * 512))

    names = {d for d, _, _ in rows}
    read = write = 0
    for dev, r, w in rows:
        # Skip if this is a partition of another listed whole device.
        if any(dev != other and dev.startswith(other) for other in names):
            continue
        read += r
        write += w
    return read, write


_NET_DEV = _net_dev_path()


def _net_bytes() -> tuple[int, int]:
    """Sum (rx_bytes, tx_bytes) across physical interfaces."""
    rx = tx = 0
    for line in _NET_DEV.read_text().splitlines():
        if ":" not in line:
            continue
        name, _, data = line.partition(":")
        name = name.strip()
        if name.startswith(_SKIP_NET_PREFIXES):
            continue
        f = data.split()
        rx += int(f[0])      # rx bytes
        tx += int(f[8])      # tx bytes
    return rx, tx


def sample() -> dict:
    """Return current host metrics. Rates are computed against the prior call."""
    global _prev
    now = time.monotonic()
    idle, total = _cpu_totals()
    mem = _meminfo()
    dr, dw = _disk_bytes()
    nrx, ntx = _net_bytes()

    cur = {"t": now, "cpu_idle": idle, "cpu_total": total,
           "disk_r": dr, "disk_w": dw, "net_rx": nrx, "net_tx": ntx}

    cpu_pct = 0.0
    disk_r = disk_w = net_rx = net_tx = 0.0
    with _lock:
        prev = _prev
        _prev = cur

    if prev:
        dt = cur["t"] - prev["t"]
        dtotal = cur["cpu_total"] - prev["cpu_total"]
        if dtotal > 0:
            didle = cur["cpu_idle"] - prev["cpu_idle"]
            cpu_pct = max(0.0, min(100.0, (1 - didle / dtotal) * 100))
        if dt > 0:
            disk_r = max(0.0, (cur["disk_r"] - prev["disk_r"]) / dt)
            disk_w = max(0.0, (cur["disk_w"] - prev["disk_w"]) / dt)
            net_rx = max(0.0, (cur["net_rx"] - prev["net_rx"]) / dt)
            net_tx = max(0.0, (cur["net_tx"] - prev["net_tx"]) / dt)

    mem_total = mem.get("MemTotal", 0)
    mem_avail = mem.get("MemAvailable", mem.get("MemFree", 0))
    mem_used = mem_total - mem_avail

    return {
        "cpu_pct": round(cpu_pct, 1),
        "mem_total": mem_total,
        "mem_used": mem_used,
        "mem_pct": round(mem_used / mem_total * 100, 1) if mem_total else 0.0,
        "disk_read_bps": round(disk_r),
        "disk_write_bps": round(disk_w),
        "net_rx_bps": round(net_rx),
        "net_tx_bps": round(net_tx),
    }
