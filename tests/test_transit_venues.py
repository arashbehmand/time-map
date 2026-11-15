"""The venue extract's pure parts (transit/venues.py, #19): which OpenStreetMap features become
meetup venues and where each is pinned, on hand-made GeoJSON features (no osmium)."""
import json
import math

from transit import build
from transit import venues as tv

M_LAT = 1 / 111_195
M_LON = M_LAT / math.cos(math.radians(51.5))


def point(fid, lon, lat, **tags):
    return {"type": "Feature", "id": fid, "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": tags}


def square(fid, lon, lat, side_m, **tags):
    """A square park or building, side_m metres a side, south-west corner at (lon, lat)."""
    dx, dy = side_m * M_LON, side_m * M_LAT
    ring = [[lon, lat], [lon + dx, lat], [lon + dx, lat + dy], [lon, lat + dy], [lon, lat]]
    return {"type": "Feature", "id": fid, "geometry": {"type": "MultiPolygon", "coordinates": [[ring]]},
            "properties": tags}


def test_named_public_venues_of_the_five_kinds_only():
    feats = [point("n1", -0.1, 51.5, amenity="pub", name="The Lamb"),
             point("n2", -0.1, 51.51, amenity="cafe"),                                 # no name
             point("n3", -0.1, 51.52, amenity="pub", name="Members Only", access="private"),
             point("n4", -0.1, 51.53, amenity="bar", name="Bar Italia"),
             square("a10", -0.12, 51.5, 20, amenity="bar", name="Gordon's"),           # way 5
             point("n5", -0.1, 51.54, amenity="restaurant", name="Rules"),
             point("n6", -0.1, 51.55, amenity="nightclub", name="Fabric"),
             point("n7", -0.1, 51.56, leisure="park", name="Pocket Park")]             # no area
    got = tv.features_to_venues(feats, [])
    assert [(v.id, v.kind, v.name) for v in got] == [
        ("node/1", "pub", "The Lamb"), ("node/4", "bar", "Bar Italia"), ("way/5", "bar", "Gordon's"),
        ("node/5", "restaurant", "Rules")]
    # A building is pinned inside it.
    gordons = got[2]
    assert -0.12 < gordons.lon < -0.12 + 20 * M_LON and 51.5 < gordons.lat < 51.5 + 20 * M_LAT


def test_a_venue_drawn_twice_is_kept_once_under_its_lowest_id():
    feats = [square("a40", -0.1, 51.5, 20, amenity="pub", name="The Lamb"),          # way 20
             point("n9", -0.1 + 5 * M_LON, 51.5 + 5 * M_LAT, amenity="pub", name="the lamb"),
             point("n3", -0.1 + 2000 * M_LON, 51.5, amenity="pub", name="The Lamb"),  # another one
             point("n4", -0.1, 51.5, amenity="cafe", name="The Lamb")]                # another kind
    got = tv.features_to_venues(feats, [])
    assert [(v.id, v.kind) for v in got] == [("node/3", "pub"), ("node/9", "pub"), ("node/4", "cafe")]


def test_a_park_needs_half_a_hectare_and_is_pinned_at_the_edge_nearest_a_station():
    small = square("a21", -0.1, 51.5, 70.6, leisure="park", name="Small")       # 4,984 m²
    big = square("a23", -0.1, 51.51, 71.0, leisure="park", name="Big")          # 5,041 m²
    station = (-0.1 + 35 * M_LON, 51.51 - 300 * M_LAT)                           # 300 m south
    got = tv.features_to_venues([small, big], [station])
    assert [(v.id, v.name) for v in got] == [("relation/11", "Big")]
    park = got[0]
    # The middle of the south edge: the point of the park nearest the station.
    assert abs(park.lat - 51.51) < 1e-6 and abs(park.lon - station[0]) < 1e-6
    # No station within 800 m: a point inside the park.
    far = tv.features_to_venues([big], [(station[0], 51.51 - 900 * M_LAT)])[0]
    assert 51.51 < far.lat < 51.51 + 71 * M_LAT


def test_osmium_ids_become_osm_ids():
    assert tv.osm_id("n123") == "node/123" and tv.osm_id("w7") == "way/7" and tv.osm_id("r9") == "relation/9"
    assert tv.osm_id("a20") == "way/10" and tv.osm_id("a41") == "relation/20"


def test_geojson_lines_with_record_separators():
    lines = ["\x1e" + json.dumps(point("n1", 0, 51, amenity="pub", name="A")) + "\n", "\n",
             json.dumps(point("n2", 0, 51, amenity="pub", name="B"))]
    assert [f["id"] for f in tv.parse_lines(lines)] == ["n1", "n2"]


def test_names_are_cleaned_and_capped():
    assert tv.clean_name("  The\tOld\x07  Bell ") == "The Old Bell"
    assert len(tv.clean_name("y" * 300)) == tv.NAME_MAX


def test_the_order_is_by_kind_then_id_whatever_the_input_order():
    feats = [point(f"n{i}", -0.1 + i * 0.01, 51.5, amenity=k, name=f"V{i}")
             for i, k in enumerate(["cafe", "pub", "restaurant", "bar", "pub"])]
    a = tv.features_to_venues(feats, [])
    b = tv.features_to_venues(feats[::-1], [])
    assert a == b and [v.kind for v in a] == ["pub", "pub", "bar", "cafe", "restaurant"]


def test_written_files_record_the_csv_s_hash(tmp_path):
    rows = tv.features_to_venues([point("n1", -0.1, 51.5, amenity="pub", name="The Lamb")], [])
    info = tv.write(tmp_path, rows, {"osm_clip_sha256": "c" * 64})
    assert info["csv"]["sha256"] == tv._sha256(tmp_path / "venues.csv")
    assert json.loads((tmp_path / "venues.json").read_text())["counts"]["pub"] == 1
    assert (tmp_path / "venues.csv").read_text().splitlines()[0] == "id,kind,name,lon,lat"


def test_venues_is_a_stage_after_walksheds_and_before_the_manifest():
    assert build.STAGES.index("walksheds") < build.STAGES.index("venues") < build.STAGES.index("manifest")
    assert "venues" in build.RUNNERS and build.STAGE_CODE["venues"] == ["venues.py"]


def test_a_station_standing_in_a_park_is_the_park_s_pin():
    big = square("a23", -0.1, 51.51, 200, leisure="park", name="Big")
    inside = (-0.1 + 50 * M_LON, 51.51 + 60 * M_LAT)
    park = tv.features_to_venues([big], [(-0.1 + 100 * M_LON, 51.51 - 300 * M_LAT), inside])[0]
    assert abs(park.lon - inside[0]) < 1e-6 and abs(park.lat - inside[1]) < 1e-6


def test_one_venue_exactly_fifty_metres_from_its_twin_is_kept_once(monkeypatch):
    monkeypatch.setattr(tv, "_metres", lambda a, b: 50.0)
    pair = [tv.Venue("node/2", "cafe", "Kaffeine", -0.1, 51.5), tv.Venue("node/1", "cafe", "kaffeine", -0.1, 51.5)]
    assert [v.id for v in tv.dedupe(pair)] == ["node/1"]
    monkeypatch.setattr(tv, "_metres", lambda a, b: 50.01)
    assert len(tv.dedupe(pair)) == 2
