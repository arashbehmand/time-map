"""
The stations-only London time map (issue #6, part L-01).

An offline batch job over one transit build (`transit/out/<build>/`): every usable station is
placed so that map distance follows travel time, by weighted SMACOF with a weak spring of
strength λ to each station's real position, solved for a sweep of λ from the real map to the
pure time map. It writes a report and a local viewer.

The full London time map (#6, L-02) sits beside it: `python -m transit.layout.timemap` lays out
every station and 300 points between them on a spring grid that can't fold, and carries the land
along (timemap.py says how).

The solves and their measures are numpy and the standard library only; only the L-02 land
(land.py) needs shapely and the osmium tool, and only the L-02 job imports it. Nothing here is
imported by the web app or by the table build, and nothing it writes may go into git or onto the
site (#6's rule for its layouts, whatever the build's licence).
"""
