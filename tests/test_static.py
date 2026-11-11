"""Attribution, pinned MapLibre with integrity hashes, and a matching service-worker cache."""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent / "static"
PAGES = ["meetup/index.html", "warp/index.html"]
MAPLIBRE = re.compile(r'(?:src|href)="(https://unpkg\.com/maplibre-gl@[^"]+)"')


@pytest.mark.parametrize("page", PAGES)
def test_maplibre_is_pinned_with_integrity(page):
    html = (ROOT / page).read_text()
    tags = re.findall(r"<(?:script|link)[^>]*maplibre-gl[^>]*>", html, re.S)
    assert len(tags) == 2
    for tag in tags:
        assert "maplibre-gl@4.7.1/" in tag
        assert re.search(r'integrity="sha384-[A-Za-z0-9+/=]{64}"', tag)
        assert 'crossorigin="anonymous"' in tag


def test_service_worker_caches_the_pinned_urls():
    sw = (ROOT / "sw.js").read_text()
    for page in PAGES:
        for url in MAPLIBRE.findall((ROOT / page).read_text()):
            assert f'"{url}"' in sw
    assert "maplibre-gl@4/" not in sw
    assert 'mode: "cors"' in sw
    assert 'CACHE_NAME = "time-map-v2"' in sw


def test_meetup_attribution():
    js = (ROOT / "meetup/app.js").read_text()
    for text in ("© Mapbox", "© OpenStreetMap", "Improve this map"):
        assert text in js
    assert "attribution: ATTRIBUTION" in js
    assert "compact: false" in js
    assert "MapboxLogoControl" in js


def test_warp_attribution_is_outside_the_hidden_map():
    html = (ROOT / "warp/index.html").read_text()
    for text in ("© Mapbox", "© OpenStreetMap", "Improve this map", 'id="mapbox-logo"'):
        assert text in html
    css = (ROOT / "warp/style.css").read_text()
    assert re.search(r"#attribution\s*{[^}]*z-index:\s*5", css)
    assert "attributionControl: false" in (ROOT / "warp/app.js").read_text()


def test_logo_is_plain_svg():
    svg = (ROOT / "icons/mapbox-logo.svg").read_text()
    assert svg.lstrip().startswith("<?xml") and "<svg" in svg
    assert "<script" not in svg.lower() and "onload" not in svg.lower()


@pytest.mark.parametrize("page", ["meetup/app.js", "warp/app.js"])
def test_pages_search_through_our_server_on_submit_only(page):
    js = (ROOT / page).read_text()
    assert "nominatim.openstreetmap.org" not in js
    assert '"/api/geocode"' in js
    assert "geoDebounce" not in js
