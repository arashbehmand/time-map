"""
The stations-only London time map (issue #6, part L-01).

An offline batch job over one transit build (`transit/out/<build>/`): every usable station is
placed so that map distance follows travel time, by weighted SMACOF with a weak spring of
strength λ to each station's real position, solved for a sweep of λ from the real map to the
pure time map. It writes a report and a local viewer.

numpy and the standard library only. Nothing here is imported by the web app or by the table
build, and nothing it writes may go into git: the tables are not licensed for publication.
"""
