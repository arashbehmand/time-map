# One-off check: does UK2GTFS convert TfL's Underground TransXChange cleanly? (issue #1)
# Not part of the build. Build and run from the repo root:
#   docker build -t time-map-uk2gtfs transit/uk2gtfs
#   docker run --rm -v "$PWD/transit/data:/data" time-map-uk2gtfs /data/raw/<build>/tfl/journey-planner-timetables.zip /data/work/<build>/uk2gtfs
FROM rocker/geospatial:4.6.1
# UK2GTFS is on GitHub only; pinned to a commit (master on 2026-10-07).
RUN R -q -e "install.packages('remotes'); remotes::install_github('ITSLeeds/UK2GTFS@d56df9a8a933b25383a8a98ba91a060181f101ba', upgrade = 'never')"
COPY check_underground.R /check_underground.R
ENTRYPOINT ["Rscript", "/check_underground.R"]
