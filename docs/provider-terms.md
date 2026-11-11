# Provider terms: what they say and where we stand

Read on 2026-11-11. Quotes are copied from the pages linked. Re-read them before relying on
this note, because the terms change.

This note records the terms. It does **not** claim that our current use complies with them.

## Mapbox Isochrone API

Source: <https://docs.mapbox.com/api/navigation/isochrone/>

> Results must be displayed on a Mapbox map using one of the Mapbox libraries or SDKs.

> The Isochrone API is limited to 300 requests per minute.

> The Isochrone API can support a maximum of 4 isochrone contours per request.

> The maximum time that can be specified for an isochrone contour is 60 minutes.

Pricing (<https://www.mapbox.com/pricing>): "Up to 100,000 Free" monthly requests, then $2.00
per 1,000.

**Where we stand: known conflict.** We draw Mapbox isochrones with MapLibre GL JS (meetup page)
and with our own WebGL canvas (warp page). Neither is a Mapbox library or SDK. Changing only the
base map would not fix this, because the clause is about how Mapbox-made isochrones are shown.

**Decision (owner, 2026-11-11):** keep the current behaviour for now, and handle permission
from Mapbox, or a replacement isochrone source, as separate work. The options are:

- written permission from Mapbox;
- isochrones from a source with compatible terms (for example a self-hosted OSM router);
- not showing Mapbox-made isochrones.

The owner decided not to open a separate issue for this (2026-11-11). This note is the
record.

## Mapbox Matrix API

Source: <https://docs.mapbox.com/api/navigation/matrix/>

> For the mapbox/driving, mapbox/walking, and mapbox/cycling profiles: Maximum 25 input
> coordinates per request. Maximum 60 requests per minute.
> For the mapbox/driving-traffic profile: Maximum 10 input coordinates per request.
> Maximum 30 requests per minute.

> The Matrix API handles bulk requests of widely varying sizes, so billing is tracked by the
> number of elements returned instead of the number of requests made.

> The minimum number of elements per request is two.

Pricing: "Up to 100,000 Free" monthly matrix elements, then $2.00 per 1,000.

We did not find a display requirement for Matrix results on that page. We show only the
resulting times and a meeting point.

## Mapbox Vector Tiles API and Fonts API

Sources: <https://docs.mapbox.com/api/maps/vector-tiles/>, <https://docs.mapbox.com/api/maps/fonts/>

Vector Tiles pricing: "Up to 200,000 Free" monthly tile requests, then $0.25 per 1,000 (for
use outside Mapbox GL JS map loads).

Fonts API (glyph) requests: we found no Fonts line on the pricing page or the Fonts API
page. The owner reports that glyph requests have never been charged on this account, which
has loaded glyphs this way since before this change. The glyph cap (`GLYPHS_MONTHLY_BUDGET`,
default 180,000 a month) is therefore an abuse bound. A refused glyph range is served empty,
so maps draw without labels. It can't be refused with an error instead, because MapLibre
drops the whole vector tile when a glyph range fails to load.

**Not read:** the product terms on caching or proxying tile and glyph responses.
<https://www.mapbox.com/legal/product-terms> did not render its service-specific sections
when we fetched it. We proxy tiles and glyphs through our server with browser
`Cache-Control` headers and keep no server-side copy. Confirm this is allowed before adding
a server-side tile cache.

## Attribution

Source: <https://docs.mapbox.com/help/dive-deeper/attribution/>

> Maps using Mapbox map styles or data supplied by Mapbox must display both the Mapbox logo
> and text attribution.

> The text attribution contains at least three links: © Mapbox, © OpenStreetMap and
> Improve this map.

> If you are using Mapbox maps with another open source library, like Leaflet or OpenLayers,
> the Mapbox logo and attribution are still required.

> You may not style the Mapbox logo.

**Where we stand:** both pages show the logo and the three links. The meetup page shows them
in MapLibre's attribution control (not collapsed) plus a logo control. The warp page uses
fixed elements above the canvases, because its MapLibre map is hidden. The logo file
(`static/icons/mapbox-logo.svg`) is the one embedded in that guide. The guide says the
"Improve this map" link *should* carry the map's coordinates; ours does not.

OpenStreetMap credit (<https://www.openstreetmap.org/copyright>) is covered by the
"© OpenStreetMap" link. Add the TfL and rail credits when those sources are added (see #1).

## Nominatim (place search)

Source: <https://operations.osmfoundation.org/policies/nominatim/>

> No heavy uses (an absolute maximum of 1 request per second).

> Provide a valid HTTP Referer or User-Agent identifying the application (stock User-Agents
> as set by http libraries will not do).

> Results must be cached on your side.

> Auto-complete search: This is not yet supported by Nominatim and you must not implement
> such a service on the client side using the API.

**Where we stand:**

- Searches go through `POST /api/geocode`, on submit only.
- The whole site shares one queue, with at least 1.1 s between requests.
- Results are cached for 24 h (not-found results for 1 h).
- The User-Agent comes from `GEOCODER_USER_AGENT`. Before deploying, set it to include a
  contact address.
- If traffic grows past occasional use, move to a provider that allows it.
