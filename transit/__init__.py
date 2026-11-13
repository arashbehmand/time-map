"""
Offline London public-transport tables (issue #1, T-01).

Sources -> GTFS -> R5 network -> per-window travel-time tables, station walksheds and a
manifest, plus a gate against TfL Journey Planner. The web app imports only transit.tables
(server/transit/provider.py), which needs numpy and h3 and nothing else.
Modules that need r5py, geopandas or the network import them inside functions, so the pure
helpers (transit.tables) and the tests run without Java.
"""
