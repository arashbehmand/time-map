"""
Runtime limits, read from the environment.

All counts are per single process. The app must run as one worker on one replica;
more would multiply every limit (see README, "Limits").
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def on_railway() -> bool:
    return any(os.environ.get(k) for k in
               ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_PROJECT_ID"))


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


DEFAULT_USAGE_DB_PATH = "usage.sqlite3"

# Monthly free tiers, as Mapbox publishes them, with 10% headroom.
#   Matrix: 100,000 elements; Vector Tiles API: 200,000 requests (mapbox.com/pricing).
#   Isochrone: 100,000 requests (mapbox.com/pricing).
#   Fonts: the owner reports glyph requests have never been charged on this account. The cap
#   is set like the tiles cap, as an abuse bound, not a billing one. A refused glyph range is
#   served empty (maps render without labels), never as an error.
DEFAULT_MONTHLY = {
    "matrix": 90_000,
    "isochrone": 90_000,
    "tiles": 180_000,
    "glyphs": 180_000,
    "geocode": 40_000,
}

# Per-token rate windows (requests per minute). Matrix limits are per profile group.
DEFAULT_RATES = {
    "matrix": 60,
    "matrix:driving-traffic": 30,
    "isochrone": 300,
    "tiles": 600,
    "glyphs": 600,
    "geocode": 60,
}

DEFAULT_IN_FLIGHT = {"matrix": 3, "isochrone": 6, "tiles": 16, "glyphs": 8, "geocode": 1}

DEFAULT_PER_IP = {
    "solve": 6,
    "isochrones": 20,
    "warp_params": 30,
    "geocode": 10,
    "tiles": 400,
    "glyphs": 300,
}


@dataclass(frozen=True)
class Settings:
    router_provider: str = "mapbox"
    usage_db_path: str = DEFAULT_USAGE_DB_PATH
    monthly_budget: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_MONTHLY))
    daily_budget: dict[str, int] = field(default_factory=dict)  # empty → monthly // 28
    rates_per_min: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_RATES))
    in_flight: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_IN_FLIGHT))
    per_ip_per_min: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_PER_IP))
    rate_wait_s: float = 10.0
    in_flight_wait_s: float = 10.0
    client_ip_header: str = "x-forwarded-for"
    trusted_proxy_hops: int = 0
    per_ip_max_keys: int = 10_000
    max_solve_elements: int = 4_000
    # One solve at a time, so the check that its Matrix requests fit in the free rate slots
    # cannot race another solve.
    max_concurrent_solves: int = 1
    solve_wait_s: float = 10.0
    # Most of one API's daily budget a single client (IP, or IPv6 /64) may use.
    client_daily_share: float = 0.25
    max_body_bytes: int = 16 * 1024
    usage_log_interval_s: float = 900.0
    geocoder_user_agent: str = "time-map/1.0 (+https://github.com/arashbehmand/time-map)"
    geocode_spacing_s: float = 1.1
    geocode_max_waiting: int = 5

    def daily(self, api: str) -> int:
        if api in self.daily_budget:
            return self.daily_budget[api]
        return self.monthly_budget.get(api, 0) // 28

    def monthly(self, api: str) -> int:
        return self.monthly_budget.get(api, 0)

    def with_(self, **kw) -> Settings:
        return replace(self, **kw)

    @classmethod
    def from_env(cls) -> Settings:
        monthly = {
            api: _int(f"{api.upper()}_MONTHLY_BUDGET", v) for api, v in DEFAULT_MONTHLY.items()
        }
        daily = {
            api: _int(f"{api.upper()}_DAILY_BUDGET", -1) for api in DEFAULT_MONTHLY
        }
        daily = {k: v for k, v in daily.items() if v >= 0}
        per_ip = {
            g: _int(f"PER_IP_{g.upper()}_PER_MIN", v) for g, v in DEFAULT_PER_IP.items()
        }
        return cls(
            router_provider=os.environ.get("ROUTER_PROVIDER", "mapbox"),
            usage_db_path=os.environ.get("USAGE_DB_PATH", DEFAULT_USAGE_DB_PATH),
            monthly_budget=monthly,
            daily_budget=daily,
            per_ip_per_min=per_ip,
            client_ip_header=os.environ.get("CLIENT_IP_HEADER", "x-forwarded-for").lower(),
            # Railway's edge proxy sets X-Forwarded-For, so on Railway the right-most entry is
            # the client. Elsewhere, trust no proxy unless told to.
            trusted_proxy_hops=_int("TRUSTED_PROXY_HOPS", 1 if on_railway() else 0),
            max_solve_elements=_int("MAX_SOLVE_ELEMENTS", 4_000),
            client_daily_share=_float("CLIENT_DAILY_SHARE", 0.25),
            geocoder_user_agent=os.environ.get("GEOCODER_USER_AGENT", "").strip()
            or cls.geocoder_user_agent,
        )
