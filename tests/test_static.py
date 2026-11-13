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


def test_service_worker_caches_only_our_own_files():
    sw = (ROOT / "sw.js").read_text()
    # Versioned per deploy: the server fills in the build id (server/app.py).
    assert 'CACHE_NAME = "time-map-__BUILD__"' in sw
    assert "time-map-v2" not in sw
    # No third-party response is cached, not even the pinned MapLibre files.
    assert "unpkg.com" not in sw and "https://" not in sw
    assert "url.origin !== self.location.origin" in sw
    for path in ("/api/", "/tiles/", "/glyphs/"):
        assert f'"{path}' not in sw.split("const APP_SHELL")[1].split("];")[0]
    # Network first, past the browser's HTTP cache: the cache is read only after the
    # network fails, and a new build never stores an old file.
    body = sw.split("async function networkFirst")[1]
    assert body.index('await fetch(request, { cache: "no-cache" })') < body.index("cache.match(request)")
    assert 'new Request(path, { cache: "reload" })' in sw


def test_every_precached_file_exists():
    sw = (ROOT / "sw.js").read_text()
    shell = re.findall(r'^\s+"(/[^"]*)",$', sw.split("const APP_SHELL")[1].split("];")[0], re.M)
    assert shell
    routes = {"/": "portfolio/index.html", "/meetup/": "meetup/index.html", "/warp/": "warp/index.html"}
    for path in shell:
        rel = routes.get(path, path.lstrip("/"))
        assert (ROOT / rel).is_file(), path
    # Every module the pages import is precached, so the app shell opens offline (the map
    # itself needs the network: MapLibre and tiles are never cached).
    for page in ("warp/app.js", "meetup/app.js"):
        for imp in re.findall(r'from "([^"]+)"', (ROOT / page).read_text()):
            full = imp if imp.startswith("/") else "/" + page.rsplit("/", 1)[0] + "/" + imp.removeprefix("./")
            assert full in shell, full


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
    assert re.search(r'import \{[^}]*\bgeocode\b[^}]*\} from "/shared/api.js"', js)
    assert "geoDebounce" not in js
    shared = (ROOT / "shared/api.js").read_text()
    assert '"/api/geocode"' in shared and "nominatim.openstreetmap.org" not in shared
    # Searches run from a form's submit handler only, never on input.
    assert '"input"' not in js


def test_meetup_shows_reach_failures():
    js = (ROOT / "meetup/app.js").read_text()
    assert 'return { p, error: friendly(err) }' in js
    assert "Reach isn't available right now" in js


@pytest.mark.parametrize("page", ["meetup/index.html", "warp/index.html", "portfolio/index.html"])
def test_page_zoom_is_allowed(page):
    html = (ROOT / page).read_text()
    assert "user-scalable" not in html
    assert "maximum-scale" not in html


def test_warp_has_choices_not_parameters():
    html = (ROOT / "warp/index.html").read_text()
    assert 'id="times"' not in html and 'type="range"' not in html
    assert 'id="loading"' not in html
    js = (ROOT / "warp/app.js").read_text()
    assert "getExtension" not in js           # never relies on float-texture filtering
    assert "texelFetch" in js
    assert "alert(" not in js


# Words a first-time visitor shouldn't have to know.
JARGON = re.compile(
    r"isochrone|minimax|hybrid|\bsum\b|\bH3\b|webgl|glsl|optimi[sz]er|portfolio", re.I
)


def _visible_html_text(html: str) -> list[str]:
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    attrs = re.findall(r'\b(?:placeholder|title|aria-label|alt)="([^"]*)"', html)
    text = re.sub(r"<[^>]+>", " ", html)
    return [text, *attrs]


@pytest.mark.parametrize("page", ["meetup/index.html", "warp/index.html", "portfolio/index.html"])
def test_no_jargon_in_page_text(page):
    for chunk in _visible_html_text((ROOT / page).read_text()):
        assert not JARGON.search(chunk), JARGON.search(chunk).group(0)


@pytest.mark.parametrize("page", ["meetup/app.js", "meetup/words.js", "warp/app.js", "warp/warpmath.js",
                                  "shared/api.js"])
def test_no_jargon_in_ui_strings(page):
    js = (ROOT / page).read_text()
    js = re.sub(r"/\*.*?\*/|^\s*//.*$", "", js, flags=re.S | re.M)      # comments
    js = re.sub(r"/\* glsl \*/`.*?`", "", js, flags=re.S)                 # shader source
    strings = re.findall(r'"([^"\n]*)"|`([^`]*)`', js)
    for a, b in strings:
        s = a or b
        if " " not in s:          # one-word values: API fields, ids, objective names
            continue
        assert not JARGON.search(s), s


def test_manifest_has_no_jargon():
    assert not JARGON.search((ROOT / "manifest.json").read_text())


def test_landing_animations_stop_on_their_own():
    # Motion that starts by itself must stop within 5 seconds (WCAG 2.2.2).
    html = (ROOT / "portfolio/index.html").read_text()
    assert "infinite" not in html
    for dur, delay in re.findall(r"animation: [\w-]+ ([\d.]+)s [\w-]+ ([\d.]+)s 1 both", html):
        assert float(dur) + float(delay) < 5


# ── The mesh renderer spike (issue #5, W-01) ─────────────────────────
LAB = "warp/lab/index.html"


def test_lab_page_is_unlisted_and_credits_the_map():
    html = (ROOT / LAB).read_text()
    assert '<meta name="robots" content="noindex' in html
    for text in ("© Mapbox", "© OpenStreetMap", "Improve this map", 'id="mapbox-logo"'):
        assert text in html
    # Not in the app shell, and no page links to it.
    assert "/warp/lab" not in (ROOT / "sw.js").read_text()
    for page in [*PAGES, "portfolio/index.html"]:
        assert "lab/" not in (ROOT / page).read_text()


def test_lab_pins_its_third_party_scripts():
    html = (ROOT / LAB).read_text()
    tags = re.findall(r"<(?:script|link)[^>]*maplibre-gl[^>]*>", html, re.S)
    assert len(tags) == 2
    for tag in tags:
        assert "maplibre-gl@4.7.1/" in tag
        assert re.search(r'integrity="sha384-[A-Za-z0-9+/=]{64}"', tag)
        assert 'crossorigin="anonymous"' in tag
    deck = (ROOT / "warp/lab/backend-deck.js").read_text()
    assert '"https://unpkg.com/deck.gl@9.4.1/dist.min.js"' in deck
    assert re.search(r'DECK_SRI = "sha384-[A-Za-z0-9+/=]{64}"', deck)
    assert "s.integrity = DECK_SRI" in deck and 's.crossOrigin = "anonymous"' in deck


def test_mesh_renderer_needs_no_float_extension():
    # Float textures are sampled with NEAREST and texelFetch only (core WebGL2); extensions are
    # never asked for, only listed (caps.js reads getSupportedExtensions).
    for path in (ROOT / "shared/mesh").glob("*.js"):
        assert "getExtension" not in path.read_text(), path.name
    caps = (ROOT / "shared/mesh/caps.js").read_text()
    assert "gl.LINEAR" not in caps
    assert "TEXTURE_MIN_FILTER, gl.NEAREST" in caps and "TEXTURE_MAG_FILTER, gl.NEAREST" in caps


def test_vendored_libraries_are_listed_with_licences_and_import_relatively():
    vendor = ROOT / "shared/vendor"
    readme = (vendor / "README.md").read_text()
    folders = re.findall(r"^\| `([\w-]+)/` \|", readme, re.M)
    assert sorted(folders) == sorted(p.name for p in vendor.iterdir() if p.is_dir())
    for folder in folders:
        assert (vendor / folder / "LICENSE").is_file(), folder
        for js in (vendor / folder).glob("*.js"):
            for spec in re.findall(r'^import [^;]*? from [\'"]([^\'"]+)[\'"]', js.read_text(), re.M):
                assert spec.startswith("."), f"{folder}/{js.name} imports {spec}"
    # The mesh modules and the lab import only by relative path, so node tests can load them.
    for js in [*(ROOT / "shared/mesh").glob("*.js"), *(ROOT / "warp/lab").glob("*.js")]:
        for spec in re.findall(r'^import [^;]*? from "([^"]+)"', js.read_text(), re.M):
            assert spec.startswith("."), f"{js.name} imports {spec}"


def test_lab_spends_no_tile_budget_unless_asked():
    js = (ROOT / "warp/lab/lab.js").read_text()
    # Synthetic geometry unless ?geom=tiles; the census refuses to run without it.
    assert 'geom: Q.get("geom") === "tiles" ? "tiles" : "synthetic"' in js
    assert 'if (OPT.geom !== "tiles")' in js
    # Mapbox's credit shows only when Mapbox data is drawn; otherwise the page says synthetic.
    html = (ROOT / LAB).read_text()
    assert 'id="synthetic-note"' in html and "updateCredits" in js


def test_lab_driver_is_in_the_repo():
    driver = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "lab_headless.mjs"
    text = driver.read_text()
    assert "__lab.runSelfTest()" in text and "Runtime.evaluate" in text
