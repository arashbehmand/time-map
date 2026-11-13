# Transit data sources: what we use, versions, licences

Read on 2026-11-12. This note records the sources of the London transit tables (issue #1,
T-01) and what their terms say, in the style of [provider-terms.md](provider-terms.md). It
does **not** claim that publishing the tables is allowed: see "May the tables be published?".

Every build records the same facts for its own downloads in `transit/out/<build>/manifest.json`
(URL, size, sha256, Last-Modified, "timetable as of").

## Sources used by the build

| Source | Used for | Version / timetable as of | Calendar | Size, sha256 (build 2026-11-12) | Licence | Credit |
|---|---|---|---|---|---|---|
| [gb-transit](https://github.com/planarnetwork/gb-transit) `gtfs-rail-and-tfl.zip`, release `feed-2026-11-12` | National Rail (incl. London Overground, Elizabeth line, Heathrow Express), Underground, DLR, trams, river boats, cable car | Rail: RJTTC999.ZIP, built 2026-11-12. TfL part: TfL's timetables **as kept on 2026-11-03** (the release notes say TfL turned their download away) | 2026-11-12 to 2027-02-12 | 35,906,165 B, `4b881020…a9b307` | Rail Settlement Plan data licence (RDG); NRE Knowledgebase licence; OGL v3.0 (NaPTAN); TfL Transport Data Service terms | Powered by TfL Open Data; Contains OS data © Crown copyright and database rights 2016 and Geomni UK Map data © and database rights [2019] |
| [Bus Open Data Service](https://data.bus-data.dft.gov.uk/) `itm_london_gtfs.zip` (ITO World's GTFS of BODS, London region) | Buses (route_type 3 only) | feed_version 20261112_041209 | 2026-11-12 to 2027-09-27 | 294,813,492 B, `6157e271…ee5e8` | Open Government Licence v3.0 (DfT's listing: "Free", OGL) | Contains public sector information licensed under the Open Government Licence v3.0 |
| [Geofabrik](https://download.geofabrik.de/europe/united-kingdom/england.html) `england-latest.osm.pbf` (served as `england-261111.osm.pbf`) | Streets for walking, clipped to Greater London + 5 km | OSM data as of 2026-11-11 | n/a | 1,713,131,590 B, `ea82efa1…f475e` (md5 checked against Geofabrik's) | ODbL 1.0 | © OpenStreetMap contributors |
| ONS Regions (December 2023) BGC, E12000007 (`transit/london_region.geojson`) | The study area | December 2023 | n/a | `db0ed82c…04bf` | OGL v3.0 | Source: Office for National Statistics licensed under the OGL v3.0; Contains OS data © Crown copyright and database right 2023 |
| [GOV.UK bank holidays](https://www.gov.uk/bank-holidays.json) | Picking the service dates | 2026-11-12 | n/a | 22,207 B | OGL v3.0 | |
| TfL Unified API (`api.tfl.gov.uk`): Journey Planner, line lists, line status | The gate's reference times; the feed check's line list; Saturday closures | live | n/a | n/a | TfL Transport Data Service terms | Powered by TfL Open Data |

Downloaded but not part of the tables: TfL's own Journey Planner timetables
(`journey-planner-timetables.zip`, TransXChange, 133,106,037 B, Last-Modified 2026-11-09,
`788955e8…08ad7`), for the UK2GTFS check and as the no-account bus fallback.

**Accounts.** None of the build's downloads needed an account on 2026-11-12 (the BODS London
file downloads without a login). The gate needs a free TfL API key; see `transit/README.md`.

## Checked and kept as switches (not downloaded)

| Source | What it is | Why it is not used now |
|---|---|---|
| Interline's TfL GTFS on Transitland (`f-transport~for~london`) | GTFS of TfL's feed: Underground, DLR, trams, cable car, Clippers, Woolwich Ferry; no buses, Overground or Elizabeth line. Version "TfL 20261109", calendar 2026-10-31 to 2026-12-23, so **it is current** (the issue's question). Licence on the listing: redistribution and derived products allowed, credit "Powered by TfL Open Data" | Needs a Transitland key; gb-transit carries the same TfL data without one |
| Interline's UK National Rail GTFS on Transitland (`f-uk~rail`) | Version "UK National Rail 20261109", 2026-05-17 to 2027-05-15, 32 operators incl. London Overground and Elizabeth line. Listing: redistribution and derived products allowed, credit "Powered by National Rail Enquiries" | The switch for rail if the RSP licence does not allow publishing derived tables. Needs a Transitland key |
| UBDC "Bus Open Data Service GTFS archive" (Glasgow) | 2021-2023 archive | The record says "There are no files for this dataset available to download" |
| travelwhiz GB-Bus-Train-Metro-GTFS | Daily GB feed incl. buses | Its README (CC BY 4.0) and LICENSE file (CC0) disagree; rail from Darwin; source selection not documented |
| Older (archived) versions | gb-transit keeps 30 days of releases plus the first of each month; Transitland keeps 13-18 versions | A current version is free and needs no key. An old one would make the gate measure timetable changes, because Journey Planner only plans current and future dates |

## What the terms say (and what could not be read)

- **TfL.** The terms page (<https://tfl.gov.uk/corporate/terms-and-conditions/transport-data-service>)
  rendered only "These terms and conditions apply to TfL's free Transport Data Service."
  when fetched. gb-transit states that "TfL's open data licence makes the acknowledgement
  'Powered by TfL Open Data' a condition of using the timetables". **Not read:** TfL's own
  text on derived data, caching of Journey Planner replies and bulk API use. Read it in a
  browser before publishing tables or running the full gate (tens of thousands of requests at
  100 a minute; `python -m transit.gate --estimate` gives the number).
- **Rail (RSP data licence, Rail Delivery Group).** <https://raildata.org.uk/> returned an
  empty page when fetched. RDG's timetable page says the free weekly timetable is "for use in
  proof of concept or development work". **Not read:** the licence text, so whether derived
  tables may be published is unknown.
- **NRE Knowledgebase licence.** The Knowledgebase page says access "is enabled through
  automated registration" and the data sets "are provided free to registered users"; it gives
  no derived-data or credit terms. gb-transit used Knowledgebase station data to name and
  group stations.
- **BODS.** The BODS site refused automated fetches (403). DfT's catalogue entry for "Bus Open
  Data - download all timetable data" lists the licence as the Open Government Licence.
- **OSM.** ODbL 1.0: the tables are a derivative database, so publishing them means
  publishing them under the ODbL, with the credit.

## May the tables be published?

Not yet. Build, check and gate locally (this spike); publish only when:

1. the RSP licence (or the switch to Interline's rail GTFS) allows derived tables,
2. TfL's terms have been read for derived data, and
3. the owner agrees.

`transit/sources.toml` has `[publication] licences_cleared = false`. Until it is set to true,
the archive's `LICENSE-DATA.txt` says it is **not licensed for publication** and grants no
rights, and `python -m transit.build publish` refuses. With the flag set, publishing still
needs `--confirm` and makes a draft release only.

## Notes from gb-transit's "Using this data"

Its five warnings, and how the build handles them:

1. No identifier is a three-letter station code (stop_id is an ATCO code): the build uses
   stop_id and parent_station only.
2. stop_times reference platforms, not stations: points use the parent station.
3. Times run past 24:00:00, and services before 02:00 belong to the previous service day:
   kept as they are (valid GTFS; R5 reads them).
4. stop_headsign contains commas: the build reads with a CSV parser and drops that column.
5. service_id is only stable within one build: prep rewrites service ids per build anyway.
