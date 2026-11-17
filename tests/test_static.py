"""Attribution, pinned MapLibre with integrity hashes, and a matching service-worker cache."""
import pathlib
import posixpath
import re

import pytest

from server.app import TOOL_PATHS  # the three tools' public paths and their folders (#28)

ROOT = pathlib.Path(__file__).resolve().parent.parent / "static"
PAGES = ["meetup/index.html", "warp/index.html"]
TOOL_PAGES = ["warp/index.html", "meetup/index.html", "whole-city/index.html"]
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
    # Whole city's data is the one API path kept, after a view, network first, in its own cache
    # (which the activate step's time-map- cleanup leaves alone); a 404 from the gate drops it.
    city = sw.split("async function cityData")[1].split("\n}\n")[0]
    assert 'const CITY_CACHE = "whole-city-data";' in sw and 'const CITY_DATA = "/api/whole-city/";' in sw
    assert city.index("await fetch(request)") < city.index("cache.match(request)")
    assert "response.status === 404) await caches.delete(CITY_CACHE)" in city
    assert '"whole-city-data"' not in sw.split("activate")[1].split("});")[0]
    assert 'export const CITY_CACHE = "whole-city-data";' in (ROOT / "shared/offline.js").read_text()
    # Network first, past the browser's HTTP cache: the cache is read only after the
    # network fails, and a new build never stores an old file.
    body = sw.split("async function networkFirst")[1]
    assert body.index('await fetch(request, { cache: "no-cache" })') < body.index("cache.match(request)")
    assert 'new Request(path, { cache: "reload" })' in sw


def _static_file(path: str) -> pathlib.Path:
    """A public path → the file under static/ that serves it (server/app.py TOOL_PATHS)."""
    if path == "/":
        return ROOT / "portfolio/index.html"
    first, slash, rest = path.lstrip("/").partition("/")
    if not slash:
        return ROOT / first
    return ROOT / TOOL_PATHS.get(first, first) / (rest or "index.html")


def test_every_precached_file_exists():
    sw = (ROOT / "sw.js").read_text()
    shell = re.findall(r'^\s+"(/[^"]*)",$', sw.split("const APP_SHELL")[1].split("];")[0], re.M)
    assert shell
    for path in shell:
        assert _static_file(path).is_file(), path
    # Every module the pages import, and every module those import, is precached, so the app
    # shell opens offline (the maps themselves need the network: MapLibre, tiles and Whole
    # city's data are never cached).
    todo = ["/from-here/app.js", "/meet-up/app.js", "/whole-city/app.js"]
    seen = set()
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        seen.add(mod)
        assert mod in shell, mod
        base = mod.rsplit("/", 1)[0]
        for imp in re.findall(r'from "([^"]+)"', _static_file(mod).read_text()):
            full = imp if imp.startswith("/") else posixpath.normpath(f"{base}/{imp}")
            todo.append(full)


def test_the_tool_paths_are_the_three_tools():
    assert TOOL_PATHS == {"from-here": "warp", "meet-up": "meetup", "whole-city": "whole-city"}


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


def test_warp_hidden_controls_stay_hidden():
    # The page hides controls with the hidden attribute (the Bend button where bending isn't
    # offered, the trial layout chips without ?layouts=1). Class rules such as .btn's and
    # .chips' display would show them anyway, so hidden must win on this page.
    css = (ROOT / "warp/style.css").read_text()
    assert re.search(r"(^|\n)\[hidden\]\s*{\s*display:\s*none\s*!important;?\s*}", css)


def test_warp_page_has_every_element_the_legend_writes_to():
    # updateLegend runs on every drawn frame; an element it writes to that is missing from the
    # page throws there. W-03 (#27) added the squash note and the footnote past 45 minutes.
    html = (ROOT / "warp/index.html").read_text()
    js = (ROOT / "warp/app.js").read_text()
    body = js[js.index("function updateLegend()"):js.index("function updateCredits()")]
    ids = set(re.findall(r'\$\("([a-z-]+)"\)', body))
    assert {"gives-up", "rough-note"} <= ids
    for el in sorted(ids):
        assert f'id="{el}"' in html, el


def test_offline_cache_has_every_mesh_module():
    # The worker imports these; an offline reload serves only what the service worker listed.
    sw = (ROOT / "sw.js").read_text()
    for f in sorted((ROOT / "shared/mesh").glob("*.js")):
        assert f'"/shared/mesh/{f.name}"' in sw, f.name


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


def test_meetup_offers_public_transport_only_where_the_tables_are():
    html = (ROOT / "meetup/index.html").read_text()
    js = (ROOT / "meetup/app.js").read_text()
    # The When control is there, hidden until someone is on public transport.
    assert re.search(r'<div class="when-field" id="window-field" hidden>', html)
    assert '<select id="window-select">' in html
    # So is Where (#19): its kinds come from the tables' status, a pub unless the group picks.
    assert re.search(r'<label class="field" id="venue-field" hidden>', html)
    assert '<select id="venue-select"></select>' in html
    assert 'const DEFAULT_VENUE = "pub";' in js and 'new Option("Just a station", "station")' in js
    # The station sentence or the venue one, whichever was picked; no select without venues.
    assert '<p class="hint" id="venue-hint" hidden>' in html and '<p class="hint" id="both-hint" hidden>' in html
    assert '$("venue-field").hidden = !on || venueKinds.length === 0;' in js
    # Public transport starts hidden: it is shown from the tables' status, and offered to a
    # person only inside the area they cover.
    assert '<label class="chip" id="transit-chip" hidden><input type="radio" name="add-mode" value="transit"' in html
    assert html.count('value="transit"') == 1
    assert '$("transit-chip").hidden = !transit.available;' in js
    assert 'fetch("/api/transit/status")' in js and "insideArea(transit.coverage" in js
    # Journey links open in a new tab and send no referrer.
    assert 'rel="noopener noreferrer"' in js
    # ui.css gives .field and .chips display: flex, which beats the hidden attribute unless
    # the page says otherwise: the empty When select must not show where there are no tables.
    css = (ROOT / "meetup/style.css").read_text()
    assert ".field[hidden]" in css and ".chips[hidden]" in css and "display: none" in css
    # Every answer shows the fairest place and the quickest on average: no choice between them.
    assert 'name="objective"' not in html and "objective()" not in js


def test_meetup_share_links_keep_the_group_in_the_fragment():
    html = (ROOT / "meetup/index.html").read_text()
    js = (ROOT / "meetup/app.js").read_text()
    # The share panel waits for a group, and its exact-point boxes start unticked.
    assert '<details id="share" hidden>' in html and 'id="share-exact"' in html
    assert "checked" not in html.split('id="share-exact"')[1].split("</fieldset>")[0]
    css = (ROOT / "meetup/style.css").read_text()
    assert "#share[hidden]" in css and "#share-url[hidden]" in css
    # The group is read from the fragment, and the fragment is taken out of the address bar.
    assert "location.hash" in js and "history.replaceState" in js
    # A link opened in this tab survives a reload in history.state, and an edit forgets it.
    assert "linkAtLoad(location.hash, history.state)" in js
    assert "forgetLink();" in js.split("function groupChanged")[1].split("\n}\n")[0]
    assert 'history.replaceState(null, "", location.href)' in js.split("function forgetLink")[1].split("\n}\n")[0]
    # A rename is an edit too, and a new link hides a link shown for the old group.
    assert js.count("forgetLink();") >= 2
    assert "hideShareUrl();" in js.split("async function openShared")[1].split("\n}\n")[0]
    assert "location.search)" in js and "?g=" not in js
    # Only this page reads a fragment; nothing sends it anywhere.
    for page in ("meetup/words.js", "meetup/share.js", "warp/app.js", "shared/api.js"):
        assert "location.hash" not in (ROOT / page).read_text(), page
    # Names are capped where they are typed, as in the link.
    assert 'maxlength="40"' in html and 'maxlength="40"' in js


def test_landing_mentions_public_transport_only_when_the_tables_are_there():
    html = (ROOT / "portfolio/index.html").read_text()
    lines = re.findall(r'<p class="transit-line"( hidden)?>', html)
    assert len(lines) == 2 and all(lines)
    assert 'import { pollStatus, statusLoading } from "/shared/transit.js";' in html
    assert "expired" not in html                               # nothing expires (#30)
    assert ".transit-line[hidden]" in html
    # The status comes from one shared helper, not from another tool's folder (#28).
    assert "/warp/" not in html and "views.js" not in html
    assert 'url = "/api/transit/status"' in (ROOT / "shared/transit.js").read_text()


def test_landing_lists_three_tools_and_never_links_to_a_missing_map():
    html = (ROOT / "portfolio/index.html").read_text()
    main = html.split('<main class="tools">')[1].split("</main>")[0]
    for name, path in (("From here", "/from-here/"), ("Meet up", "/meet-up/")):
        assert f'<a href="{path}" class="tool">' in main and f"<h2>{name}</h2>" in main
    # Whole city is an <a> with no href (not a link) until the server says its map can be shown
    # here; then it gains the href, in place, so its animation isn't restarted.
    assert '<a class="tool" id="city-card">' in main and "<h2>Whole city</h2>" in main
    assert 'href="/whole-city/"' not in html and "aria-disabled" not in html
    assert 'document.getElementById("city-card").href = "/whole-city/";' in html
    assert "if (city && city.available)" in html and "replaceChildren" not in html
    # While it is still loading, the card keeps asking, then says so in words.
    assert "statusLoading(s)" in html and "still getting ready" in html
    # One sentence and one animation each.
    assert main.count('class="say"') == 3 and main.count("<svg") == 3
    assert html.count("<title>Minutes Away</title>") == 1 and "<h1>Minutes Away</h1>" in html


@pytest.mark.parametrize("page", ["meetup/index.html", "warp/index.html", "portfolio/index.html",
                                  "whole-city/index.html"])
def test_page_zoom_is_allowed(page):
    html = (ROOT / page).read_text()
    assert "user-scalable" not in html
    assert "maximum-scale" not in html


def test_warp_has_choices_not_parameters():
    html = (ROOT / "warp/index.html").read_text()
    assert 'id="times"' not in html and 'type="range"' not in html
    assert 'id="loading"' not in html
    js = (ROOT / "warp/app.js").read_text()
    assert "getExtension" not in js
    assert "alert(" not in js
    # The page bends through the shared mesh renderer, which reads its float textures with
    # texelFetch and never asks for float-texture filtering.
    assert '"/shared/mesh/renderer.js"' in js
    assert "texelFetch" in (ROOT / "shared/mesh/warp.js").read_text()


# Words a first-time visitor shouldn't have to know.
JARGON = re.compile(
    r"isochrone|minimax|hybrid|\bsum\b|\bH3\b|webgl|glsl|optimi[sz]er|portfolio", re.I
)


def _visible_html_text(html: str) -> list[str]:
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    attrs = re.findall(r'\b(?:placeholder|title|aria-label|alt)="([^"]*)"', html)
    text = re.sub(r"<[^>]+>", " ", html)
    return [text, *attrs]


@pytest.mark.parametrize("page", ["meetup/index.html", "warp/index.html", "portfolio/index.html",
                                  "whole-city/index.html"])
def test_no_jargon_in_page_text(page):
    for chunk in _visible_html_text((ROOT / page).read_text()):
        assert not JARGON.search(chunk), JARGON.search(chunk).group(0)


@pytest.mark.parametrize("page", ["meetup/app.js", "meetup/words.js", "meetup/share.js", "warp/app.js",
                                  "warp/words.js", "warp/views.js", "shared/api.js", "whole-city/app.js",
                                  "whole-city/carry.js", "shared/tools.js", "shared/transit.js", "shared/nav.js",
                                  "shared/locate.js", "shared/handoff.js", "shared/offline.js"])
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


# ── One app, three tools (#28, U-03) ─────────────────────────────────
TOOLS = {"warp/index.html": ("From here", "How far can I get from here?"),
         "meetup/index.html": ("Meet up", "Where should we meet?"),
         "whole-city/index.html": ("Whole city", "What does London look like when distance means travel time?")}


@pytest.mark.parametrize("page", TOOL_PAGES)
def test_each_tool_has_its_name_its_question_and_the_app_name(page):
    html = (ROOT / page).read_text()
    name, question = TOOLS[page]
    assert f"<title>{name} · Minutes Away</title>" in html
    assert f'<p class="tool-name">{name}</p>' in html
    assert re.search(rf'<h1 class="question">{re.escape(question)}</h1>', html)
    assert '<a href="/" class="back-link">← Minutes Away</a>' in html


def test_the_old_name_is_gone_from_everything_a_visitor_sees():
    files = [p for p in ROOT.rglob("*") if p.is_file() and p.suffix in (".html", ".js", ".json", ".css")
             and "lab" not in p.relative_to(ROOT).parts and "vendor" not in p.relative_to(ROOT).parts]
    for p in files:
        text = p.read_text()
        text = re.sub(r"/\*.*?\*/|^\s*//.*$", "", text, flags=re.S | re.M)
        assert "Time-Map" not in text and "Time Map" not in text, p
    readme = (ROOT.parent / "README.md").read_text()
    assert readme.startswith("# Minutes Away") and "Time-Map" not in readme
    assert '"name": "Minutes Away"' in (ROOT / "manifest.json").read_text()


@pytest.mark.parametrize("page", TOOL_PAGES)
def test_the_other_two_tools_are_one_tap_away(page):
    html = (ROOT / page).read_text()
    nav = html.split('<nav class="switcher" aria-label="Tools">')[1].split("</nav>")[0]
    here = TOOLS[page][0]
    assert nav.count('aria-current="page"') == 1 and f'aria-current="page">{here}<' in nav
    for name, path in (("From here", "/from-here/"), ("Meet up", "/meet-up/")):
        assert f'href="{path}"' in nav and f">{name}<" in nav
    # Whole city starts as text in the other tools: it becomes a link only once the server
    # says its map can be shown (shared/nav.js), so the switcher never leads to a broken page.
    if page != "whole-city/index.html":
        assert '<span data-tool="whole-city" class="off">Whole city</span>' in nav
        assert "/whole-city/" not in html
        js = (ROOT / page.replace("index.html", "app.js")).read_text()
        assert "updateSwitcher(" in js and "watchCity(" in js
    nav_js = (ROOT / "shared/nav.js").read_text()
    assert "if (city && city.available)" in nav_js and 'a.href = "/whole-city/";' in nav_js
    # Why it isn't available is visible (short) and read out (in full); no aria-disabled on text.
    assert "cityShort(reason)" in nav_js and "cityUnavailable(reason)" in nav_js and "aria-disabled" not in nav_js


def test_the_pressed_tick_is_not_part_of_the_buttons_name():
    css = (ROOT / "shared/ui.css").read_text()
    rules = re.findall(r'\[aria-pressed="true"\]::before\s*{([^}]*)}', css)
    assert rules and all(re.search(r'content:\s*"[^"]*"\s*/\s*""', r) for r in rules), rules


def test_meet_up_keeps_its_group_for_the_trip_back():
    js = (ROOT / "meetup/app.js").read_text()
    # Saved on every change and after a shared link; restored ahead of a handed-on pin.
    assert "keepGroup();" in js.split("function handOn")[1].split("\n}\n")[0]
    # An edited example is kept too (its people keep their example flag, so aren't handed on).
    assert "every((p) => p.example)" not in js.split("function keepGroup")[1].split("\n}\n")[0]
    assert "handOn();" in js.split("async function openShared")[1].split("\n}\n")[0]
    fresh = js.split("const fresh = () => {")[1].split("};")[0]
    assert fresh.index("readGroup()") < fresh.index("HANDOFF.pin")
    # The tables' status is handled once: Whole city's own loading is left to the switcher.
    assert "cityLoading" not in js and "cityLoading" not in (ROOT / "warp/app.js").read_text()


@pytest.mark.parametrize("page", ["warp/index.html", "whole-city/index.html"])
def test_the_time_map_button_says_its_state_once(page):
    html = (ROOT / page).read_text()
    button = re.search(r'<button id="(view-toggle|bend)"[^>]*>([^<]*)</button>', html)
    assert button and button.group(2) == "Time map" and 'aria-pressed="false"' in button.group(0)
    js = (ROOT / page.replace("index.html", "app.js")).read_text()
    name = "toggleBtn" if page.startswith("warp") else "toggle"
    # Its label never changes: aria-pressed alone carries the state.
    assert not re.search(rf"setText\({name}\b|{name}\.textContent\s*=", js)
    assert f'{name}.setAttribute("aria-pressed"' in js
    for gone in ("Bend the map", "Bend by travel time", "Back to the real map", "Back to the normal map"):
        assert gone not in html and gone not in js


def test_whole_city_slider_ends_are_the_two_maps():
    html = (ROOT / "whole-city/index.html").read_text()
    slider = html.split('<div class="slider">')[1].split("</div>\n    </div>")[0]
    assert "<span>Real map</span>" in slider and "<span>Time map</span>" in slider


def test_travel_mode_is_the_same_chips_in_every_tool():
    warp = (ROOT / "warp/index.html").read_text()
    meetup = (ROOT / "meetup/index.html").read_text()
    for html, name in ((warp, "mode"), (meetup, "add-mode")):
        chips = re.findall(rf'<input type="radio" name="{name}" value="(\w+)"[^>]*/><span>([^<]+)</span>', html)
        assert chips == [("walking", "Walk"), ("cycling", "Cycle"), ("driving", "Drive"), ("transit", "Public transport")]
    assert 'id="mode-select"' not in meetup and "<select class=\"p-mode\"" not in (ROOT / "meetup/app.js").read_text()
    # Each person's way of travelling: the same chips, smaller, with the shared labels.
    js = (ROOT / "meetup/app.js").read_text()
    assert 'class="chips compact p-mode"' in js and 'import { MODE_LABEL } from "/shared/tools.js";' in js
    tools = (ROOT / "shared/tools.js").read_text()
    assert 'MODE_LABEL = { walking: "Walk", cycling: "Cycle", driving: "Drive", transit: "Public transport" }' in tools


def test_time_of_day_is_the_same_visible_control():
    for page in ("warp/index.html", "meetup/index.html"):
        html = (ROOT / page).read_text()
        field = html.split('class="when-field"')[1].split("</div>")[0]
        assert '<label class="label" for="window-select">When?</label>' in field
        assert '<select id="window-select"></select>' in field
        assert "Public transport times are for a typical hour on that day" in " ".join(field.split())
        assert "<details" not in html.split('class="when-field"')[0].rsplit("</details>", 1)[-1]
    assert 'id="more-options"' not in (ROOT / "warp/index.html").read_text()
    for page in ("warp/app.js", "meetup/app.js"):
        assert "windowOptions(" in (ROOT / page).read_text()


def test_asking_for_a_location_is_the_same_in_both_tools():
    for page in ("warp", "meetup"):
        html = (ROOT / f"{page}/index.html").read_text()
        card = html.split('id="location-card"')[1].split("</div>\n  </div>")[0]
        assert "data-yes>Use my location</button>" in card and "data-no>Keep the example</button>" in card
        assert re.search(r'<button [^>]*id="locate-btn"[^>]*>Use my location</button>', html)
        js = (ROOT / f"{page}/app.js").read_text()
        assert 'from "/shared/locate.js"' in js and "offerLocation(" in js
        assert "navigator.geolocation" not in js and "LOCATION_PREF" not in js


def test_no_page_imports_another_tools_folder():
    for page in ("meetup/app.js", "meetup/share.js", "whole-city/app.js", "portfolio/index.html"):
        text = (ROOT / page).read_text()
        assert "/warp/" not in text and "../warp/" not in text, page


def test_whole_city_loads_only_its_data_through_the_gate():
    js = (ROOT / "whole-city/app.js").read_text()
    for spec in re.findall(r'from "([^"]+)"', js):
        assert spec.startswith("/shared/") or spec == "./carry.js", spec
    for target in re.findall(r"\bload\(([^,)]*)", js):
        assert target.startswith("`/api/whole-city/${win}/") or target == "url", target
    assert sorted(re.findall(r"fetch\(([^)]*)\)", js)) == ['"/api/transit/status"', "url"]
    for word in ("XMLHttpRequest", "WebSocket", "sendBeacon", "EventSource", "/tiles/", "/glyphs/", "import("):
        assert word not in js
    html = (ROOT / "whole-city/index.html").read_text()
    assert "noindex" not in html and "Local use only" not in html and "Local use only" not in js
    assert '<link rel="stylesheet" href="/shared/ui.css" />' in html


def test_whole_city_offline_whatever_its_date():
    """Offline (#28): the kept copy is used only with no answer from the server, whatever its
    date (nothing expires, #30), only for the windows whose files were kept; a server that
    refuses Whole city makes the pages forget it."""
    off = (ROOT / "shared/offline.js").read_text()
    kept = off.split("export async function keptCityWhy")[1].split("\n}\n")[0]
    assert "valid_until" not in kept and "caches.delete(CITY_CACHE)" in kept and '"expired"' not in kept
    assert "cache.match(`/api/whole-city/${w.id}/timemap.json`)" in kept
    gate = off.split("export async function cityOrKept")[1].split("\n}\n")[0]
    assert "if (!status)" in gate and 'c.reason !== "loading") await forgetCity()' in gate
    js = (ROOT / "whole-city/app.js").read_text()
    assert "({ city: offline, reason: noNetwork } = await keptCityWhy())" in js and "if (!offline) keepCity(city, win)" in js
    # Windows are offered offline only if viewed under the kept build (tests/js/shared-tools.test.js).
    assert "viewed.includes(w.id)" in kept
    assert "await forgetCity()" in js
    # The home card and the switcher go by the same rule.
    assert "await cityOrKept(s)" in (ROOT / "portfolio/index.html").read_text()
    assert "await cityOrKept(status)" in (ROOT / "shared/nav.js").read_text()


# ── The mesh renderer spike (issue #5, W-01) ─────────────────────────
LAB = "warp/lab/index.html"


def test_lab_page_is_unlisted_and_credits_the_map():
    html = (ROOT / LAB).read_text()
    assert '<meta name="robots" content="noindex' in html
    for text in ("© Mapbox", "© OpenStreetMap", "Improve this map", 'id="mapbox-logo"'):
        assert text in html
    # Not in the app shell, and no page links to it.
    sw = (ROOT / "sw.js").read_text()
    assert "/warp/lab" not in sw and "/from-here/lab" not in sw
    for page in [*PAGES, "portfolio/index.html", "whole-city/index.html"]:
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


def test_lab_fallback_hides_the_overlay_and_skips_checks():
    css = (ROOT / "warp/lab/lab.css").read_text()
    assert re.search(r"body\.no-mesh #mesh, body\.no-mesh #overlay \{ display: none; \}", css)
    js = (ROOT / "warp/lab/lab.js").read_text()
    for runner in ("async function runSelfTestNow", "async function runBenchNow"):
        body = js.split(runner)[1].split("\n}\n")[0]
        assert "meshMissing(" in body, runner


def test_whole_city_has_a_toggle_per_way_of_travelling():
    """Travel modes (#23): Train and tube is on; Walking, Bus and Car start disabled and are enabled where
    the build has them (modes.json lists them available)."""
    html = (ROOT / "whole-city/index.html").read_text()
    modes = html.split('<fieldset id="modes"')[1].split("</fieldset>")[0]
    buttons = re.findall(r'<button class="btn" type="button" data-mode="(\w+)"([^>]*)>([^<]+)</button>', modes)
    assert [(m, label) for m, _, label in buttons] == [("walk", "Walking"), ("bus", "Bus"), ("train", "Train and tube"),
                                                       ("car", "Car")]
    attrs = {m: a for m, a, _ in buttons}
    for m in ("walk", "bus", "car"):
        assert 'aria-pressed="false"' in attrs[m] and "disabled" in attrs[m] and "title" not in attrs[m], m
    assert "Coming next" not in html
    assert 'aria-pressed="true"' in attrs["train"] and "disabled" not in attrs["train"]
    # The Metropolitan line isn't special: rail and tube are one layer that follows Train and tube.
    assert 'id="line"' not in html and "Metropolitan line" not in html
    js = (ROOT / "whole-city/app.js").read_text()
    assert 'if (city.modes && city.modes[win])' in js
    assert "load(`/api/whole-city/${win}/modes/modes.json`" in js
    assert "load(`/api/whole-city/${win}/modes/${index.combos[id].file}`" in js
    # A mode is enabled only as modes.json says (carry.js modeAvailability), the switch keeps all four
    # modes' state, and a switch takes its lock before anything loads.
    assert "b.disabled = busy || !available[b.dataset.mode].on;" in js
    assert "for (const m of MODES) { if (next.has(m)) on.add(m); else on.delete(m); }" in js
    body = js.split("async function switchModes")[1].split("\n}\n")[0]
    assert body.index("setBusy(true)") < body.index("await loadCombo")
    assert 'if (name === "rail" && !modes.has("train")) continue;' in js
    assert 'if (name === "road" && !modes.has("car")) continue;' in js


def test_the_travel_modes_are_kept_and_forgotten_with_the_map():
    """Offline (#28): the travel modes' files are Whole city data like the map; a build change drops
    them, whatever combinations the build had (everything under the window's modes/)."""
    sw = (ROOT / "sw.js").read_text()
    assert "/modes/" not in sw
    off = (ROOT / "shared/offline.js").read_text()
    assert "const prefix = `/api/whole-city/${win}/modes/`;" in off and "await cache.keys()" in off
    assert off.count("await dropModes(cache, ") == 2                     # keepCity's old windows and forgetModes
    assert "combo-car" not in off
