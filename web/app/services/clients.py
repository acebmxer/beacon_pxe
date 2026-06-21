"""Recent PXE clients, parsed from dnsmasq's DHCP log.

Works in BOTH dnsmasq modes -- including proxyDHCP, where no lease file exists
(your existing DHCP server hands out addresses) and the log is the only record
that a client asked us to boot. dnsmasq runs with `log-dhcp` and is configured to
write to a file on the shared `dnsmasqconf` volume (see services/dnsmasq.py and
docker-compose.yml); we read the tail of that file here.

We don't keep a daemon tailing the file -- the dashboard polls, and parsing the
last slice of a log on each poll is cheap and stateless.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .. import config

LOG_PATH = config.DNSMASQ_DIR / "dnsmasq.log"

# How much of the tail to parse, and how long a client counts as "active".
_TAIL_BYTES = 256 * 1024

# A client's MAC is taken ONLY from dnsmasq's authoritative request lines:
#   "<txid> PXE(eth0) aa:bb:cc:dd:ee:ff proxy"
#   "<txid> DHCPDISCOVER(eth0) aa:bb:cc:dd:ee:ff"
# Crucially NOT from "option: 60 vendor-class" (which logs the ASCII of
# "PXEClient" as 50:58:45:...) or "option: 97 client-machine-id" (a GUID) --
# both look like a MAC to a naive regex and would invent phantom clients.
_CLIENT_MAC_RE = re.compile(
    r"\b(?:PXE|DHCP[A-Z]+)\([^)]*\)\s+([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})\b")
# A client IP from a real DHCP assignment (full-DHCP mode):
#   "<txid> DHCPACK(eth0) 192.168.1.50 aa:bb:.."  -- proxyDHCP never assigns one.
_CLIENT_IP_RE = re.compile(
    r"\bDHCP(?:ACK|OFFER|REQUEST)\([^)]*\)\s+(\d{1,3}(?:\.\d{1,3}){3})\b")
_NAME_RE = re.compile(r"client provides name:?\s*(\S+)")
# Syslog-style timestamp dnsmasq emits, e.g. "Jun 20 14:03:11".
_TS_RE = re.compile(r"^(\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2})")
# dnsmasq tags a multi-line transaction with an id after the pid:
#   "... dnsmasq-dhcp[1]: 7 DHCPDISCOVER(eth0) aa:bb:.."  ->  txid "7"
_TXID_RE = re.compile(r"dnsmasq-dhcp\[\d+\]:\s+(\d+)\s")
# Map dnsmasq client-arch wording (used in PXE lines AND the "tags:" line) to a
# friendly firmware label. Tags are lowercase (efi64); PXE service names are
# mixed-case (X86-64_EFI) -- match both.
_ARCH = {
    "x86pc": "BIOS",
    "x86-64_efi": "UEFI",
    "bc_efi": "UEFI",
    "pc_efi": "UEFI",
    "efi64": "UEFI",
    "efi32": "UEFI",
    "efibc": "UEFI",
}


def _parse_ts(line: str) -> datetime | None:
    m = _TS_RE.match(line)
    if not m:
        return None
    try:
        # dnsmasq logs without a year; assume the current one.
        dt = datetime.strptime(m.group(1), "%b %d %H:%M:%S")
        return dt.replace(year=datetime.now().year)
    except ValueError:
        return None


def recent(limit: int = 25) -> list[dict]:
    """Return recently-seen PXE clients, most-recent first.

    One entry per MAC, enriched with the last hostname, requested IP, firmware
    type and whether we served it a boot file -- whatever the log revealed.
    """
    try:
        with open(LOG_PATH, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_BYTES))
            text = fh.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return []

    # dnsmasq logs one client request as several lines sharing a transaction id;
    # the MAC appears on some lines, the hostname / arch / boot file on others.
    # So we first bucket fields by transaction id, learn each transaction's MAC,
    # then fold every transaction into a per-MAC client record.
    # dnsmasq reuses small transaction ids over time, so a bucket holds only the
    # currently-open transaction for an id; when a line shows a different MAC for
    # that id we've rolled over to a new request and flush the old bucket first.
    open_tx: dict[str, dict] = {}        # txid -> in-progress record
    closed: list[dict] = []              # finished records, MAC attached

    def _flush(txid: str):
        rec = open_tx.pop(txid, None)
        if rec and rec.get("mac"):
            closed.append(rec)

    for line in text.splitlines():
        txid_m = _TXID_RE.search(line)
        if not txid_m:
            continue
        txid = txid_m.group(1)
        mac_m = _CLIENT_MAC_RE.search(line)
        mac = mac_m.group(1).lower() if mac_m else None

        rec = open_tx.get(txid)
        if rec is not None and mac and rec.get("mac") and rec["mac"] != mac:
            _flush(txid)
            rec = None
        if rec is None:
            rec = open_tx[txid] = {"mac": None, "ip": "", "hostname": "",
                                   "firmware": "", "last_seen": None}

        if mac:
            rec["mac"] = mac
        ts = _parse_ts(line)
        if ts:
            rec["last_seen"] = ts
        # Only a genuine DHCP-assigned IP (full DHCP); the regex matches the
        # address on DHCPACK/OFFER/REQUEST lines, not the server-identifier the
        # proxyDHCP path logs -- so proxy clients simply have no IP here.
        ip_m = _CLIENT_IP_RE.search(line)
        if ip_m and ip_m.group(1) != "0.0.0.0":
            rec["ip"] = ip_m.group(1)
        name_m = _NAME_RE.search(line)
        if name_m:
            rec["hostname"] = name_m.group(1)
        low = line.lower()
        for tag, label in _ARCH.items():
            if re.search(rf"\b{re.escape(tag)}\b", low):
                rec["firmware"] = label
                break

    for txid in list(open_tx):
        _flush(txid)

    clients: dict[str, dict] = {}
    for t in closed:
        mac = t["mac"]
        c = clients.setdefault(mac, {
            "mac": mac, "ip": "", "hostname": "", "firmware": "",
            "last_seen": None,
        })
        # Newer transactions win for scalar fields; older ones backfill blanks.
        if t["last_seen"] and (c["last_seen"] is None or t["last_seen"] >= c["last_seen"]):
            c["last_seen"] = t["last_seen"]
            for k in ("ip", "hostname", "firmware"):
                if t[k]:
                    c[k] = t[k]
        else:
            for k in ("ip", "hostname", "firmware"):
                if t[k] and not c[k]:
                    c[k] = t[k]

    def _key(c: dict):
        return c["last_seen"] or datetime.min

    rows = sorted(clients.values(), key=_key, reverse=True)[:limit]
    now = datetime.now()
    for c in rows:
        ls = c["last_seen"]
        c["last_seen_iso"] = ls.replace(tzinfo=timezone.utc).isoformat() if ls else ""
        c["ago"] = _humanize(now - ls) if ls else "—"
    return rows


def _humanize(delta) -> str:
    s = int(delta.total_seconds())
    if s < 0:
        return "just now"
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def clear_log() -> None:
    """Truncate the dnsmasq DHCP log so the recent-clients view starts fresh.

    dnsmasq keeps its own open file handle and will simply continue appending
    after the truncation, so this is safe to do while it's running.
    """
    try:
        open(LOG_PATH, "w").close()
    except FileNotFoundError:
        pass


def count_active(rows: list[dict], window_seconds: int = 600) -> int:
    """How many of the parsed clients were seen within the window."""
    now = datetime.now()
    n = 0
    for c in rows:
        ls = c["last_seen"]
        if ls and (now - ls).total_seconds() <= window_seconds:
            n += 1
    return n
