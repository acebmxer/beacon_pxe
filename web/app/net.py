"""Resolve the real client IP, honouring a configured trusted reverse proxy.

The management port is published on all interfaces AND (in production) fronted by
a reverse proxy, so `request.client.host` is the proxy's address, not the client's
— which would make the login throttle key every request to one bucket. We trust
X-Forwarded-For only when the immediate peer is a configured proxy, and we take
the *last* hop the proxy appended (the address it saw), not the client-supplied
leftmost entry, which anyone can spoof.
"""
from __future__ import annotations

import ipaddress

from fastapi import Request

from . import config

_UNKNOWN = "unknown"


def _peer_is_trusted(peer: str) -> bool:
    if not config.TRUSTED_PROXIES:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for net in config.TRUSTED_PROXIES:
        if addr in ipaddress.ip_network(net, strict=False):
            return True
    return False


def client_ip(request: Request) -> str:
    """Best-effort real client IP for throttling/display.

    Uses X-Forwarded-For only when the socket peer is a trusted proxy; otherwise
    the socket peer itself. Returns "unknown" when neither is available.
    """
    peer = request.client.host if request.client else ""
    if peer and _peer_is_trusted(peer):
        xff = request.headers.get("x-forwarded-for", "")
        # The proxy appends the address it saw as the rightmost entry.
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        if hops:
            try:
                ipaddress.ip_address(hops[-1])
                return hops[-1]
            except ValueError:
                pass
    return peer or _UNKNOWN
