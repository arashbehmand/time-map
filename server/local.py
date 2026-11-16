"""Is a request from this machine? The only place a build whose licences are not cleared may be
served (TRANSIT_ALLOW_UNCLEARED): the tables, and the Whole city map made from them."""
from fastapi import Request

LOOPBACK = ("127.", "::1", "::ffff:127.")
FORWARDED = ("x-forwarded-for", "forwarded", "x-real-ip")


def is_local(request: Request) -> bool:
    """The request comes from this machine and through no proxy. A proxy or tunnel on the same
    machine that adds none of these headers looks local, so the override never belongs on a
    reachable server (README)."""
    host = request.client.host if request.client else ""
    settings = getattr(request.app.state, "settings", None)
    headers = (*FORWARDED, settings.client_ip_header) if settings else FORWARDED
    return host.startswith(LOOPBACK) and not any(h in request.headers for h in headers)
