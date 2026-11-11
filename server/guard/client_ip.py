from __future__ import annotations

from starlette.requests import Request


def client_ip(request: Request, header: str, hops: int) -> str:
    """
    The client address as seen by our trusted proxy.

    hops=0: the socket peer (no proxy trusted). hops=N: the Nth address from the right of
    `header`; the right-hand entries are written by proxies we trust, while the left-hand
    ones come from the client and can be forged.
    """
    peer = request.client.host if request.client else "unknown"
    if hops <= 0:
        return peer
    raw = request.headers.get(header, "")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) < hops:
        return peer
    return parts[-hops]
