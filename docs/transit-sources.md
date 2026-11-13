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
- **Rail (RSP data licence, Rail Delivery Group).** Read on 2026-11-13: RSP's *Data Licence
  Template v07-00*, a blank agreement with no parties or date. It is not the agreement that
  covers the free timetable gb-transit converts. This project has no agreement with RSP; it
  uses gb-transit's conversion. The template's Schedule leaves the parts that decide our case
  blank: the Permitted Use reads "[to be specifically defined here]" (Schedule, para 2.1), and
  the fees read £xxxx.00. What the clauses say:
  - **2.1:** a non-exclusive, non-transferable licence to use the data described in the
    Schedule, for the Permitted Use only, subject to the Agreement, any Specific Conditions,
    RSP's Journey Planning Code of Practice and its Instructions.
  - **3.1.3 and 13:** no sub-licensing or assigning. The Licensed Data may go to others only
    if the Permitted Use allows it, and each of them then needs its own data licence with RSP
    (13.2).
  - **3.1.4 and 3.3.1:** no additions to or deletions from the Licensed Data without RSP's
    written authority, and no change that makes it inaccurate. The build clips its working
    copy to London and removes doubled trips; whether that counts is a question for the real
    agreement.
  - **3.1.5:** without RSP's written permission, nothing derived from the data may show an
    inaccurate or preferential view of one train company's services against another's, or
    against another mode shown alongside them. The tables are a rough estimate that puts rail
    next to tube and bus, so this needs asking before anything is published.
  - **3.3.2:** derived output put in the public domain must carry, where RSP asks for it, a
    visible notice agreed with RSP saying that it is derived and made available under the
    Agreement's terms.
  - **3.3.3:** the Licensed Data itself must not be given to anyone else without RSP's prior
    written consent. So the source timetables are never published; at most the derived
    tables are.
  - **3.3.4:** anyone who receives the data must be told of the accuracy disclaimers and
    liability limits that RSP requires.
  - **8.1:** a Licence Fee and, if applicable, a Datafeeds Charge, "as set out in the
    Schedule". The template draws no line between commercial and non-commercial use; its
    Schedule (para 2.3) lets the licensee charge its own clients for services that use the
    data.

  RDG's timetable page says the free weekly timetable is "for use in proof of concept or
  development work". **Not read:** the agreement (or terms) that actually covers the free
  timetable, with its Schedule, and whether gb-transit may pass its conversion on. So
  whether derived tables may be published is still unknown.
- **NRE Knowledgebase licence.** The Knowledgebase page says access "is enabled through
  automated registration" and the data sets "are provided free to registered users"; it gives
  no derived-data or credit terms. gb-transit used Knowledgebase station data to name and
  group stations.
- **BODS.** The BODS site refused automated fetches (403). DfT's catalogue entry for "Bus Open
  Data - download all timetable data" lists the licence as the Open Government Licence.
- **OSM.** ODbL 1.0: the tables are a derivative database, so publishing them means
  publishing them under the ODbL, with the credit.

## May the tables be published?

Not yet. Two rules hold whatever the licences turn out to say:

- **Only derived tables, never the source data.** The feeds (gb-transit's GTFS, the BODS
  GTFS, TfL's TransXChange, any National Rail CIF/MCA files), the OSM extract and the
  prepared feeds stay on the machine that builds the tables, in `transit/data/`. RSP's clause
  3.3.3 forbids passing the rail timetable on, and none of them is ours to republish.
  `.gitignore` keeps feed, extract and table files out of git anywhere in the repo, and
  `tests/test_no_data_in_git.py` fails if one is tracked or a file is over 1 MB.
- **Derived tables go out with their notices:** the RSP notice of clause 3.3.2, worded as
  agreed with RSP if RSP asks for one; "Powered by TfL Open Data"; the rail timetable credit
  (Rail Delivery Group / Rail Settlement Plan); and the OGL, ODbL and OS credits in
  `transit/sources.toml`.

Publish only when:

1. the agreement that covers the rail timetable (not the template) allows derived tables to
   be made public, its 3.3.2 notice is settled, and RSP is content under 3.1.5 that the
   tables are approximate (or rail switches to Interline's GTFS, which allows derived
   products with the credit "Powered by National Rail Enquiries");
2. TfL's terms have been read for derived data; and
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
