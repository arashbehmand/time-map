from __future__ import annotations

import ipaddress

from starlette.requests import Request


def _bucket(raw: str) -> str | None:
    """The per-IP key for an address: IPv4 as is, IPv6 grouped by its /64. None if not an IP."""
    value = raw.strip().strip('"')
    if value.startswith("[") and "]" in value:  # [v6]:port
        value = value[1:value.index("]")]
    elif value.count(":") == 1:  # v4:port
        value = value.split(":", 1)[0]
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        # One home or one server usually gets a whole /64; count it as one client.
        return str(ipaddress.IPv6Network(f"{addr}/64", strict=False))
    return str(addr)


def client_ip(request: Request, header: str, hops: int) -> str:
    """
    The client key as seen by our trusted proxy.

    hops=0: the socket peer (no proxy trusted). hops=N: the Nth address from the right of
    `header`, read across every line of that header; the right-hand entries are written by
    proxies we trust, while the left-hand ones come from the client and can be forged.
    Anything that is not an IP address falls back to the socket peer.
    """
    peer_raw = request.client.host if request.client else ""
    peer = _bucket(peer_raw) or peer_raw or "unknown"
    if hops <= 0:
        return peer
    # getlist: a request can carry the header on several lines; headers.get() returns only the first.
    lines = request.headers.getlist(header)
    parts = [p.strip() for p in ",".join(lines).split(",") if p.strip()]
    if len(parts) < hops:
        return peer
    return _bucket(parts[-hops]) or peer
