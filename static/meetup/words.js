// The sentences the meetup page shows for an answer, and for "no place works".
// Pure functions, no page access, so they can be tested on their own (tests/js/words.test.js).

import { minutesText } from "../shared/tools.js";

// Minutes in the words every tool uses (#28): "24 min", or "under a minute".
export const mins = minutesText;

// "on Baker Street", or "" when the place has no street name.
export function onStreet(c) {
  return c && typeof c.name === "string" && c.name.trim() ? `on ${c.name.trim()}` : "";
}

export function headline(best, objectiveValue) {
  const where = onStreet(best);
  const meet = where ? `Meet ${where} (★ on the map).` : "Meet here (★ on the map).";
  return objectiveValue === "sum"
    ? `${meet} The average trip is ${mins(best.mean_time_min)}; the longest is ${mins(best.max_time_min)}.`
    : `${meet} Everyone gets there within ${mins(best.max_time_min)}.`;
}

export function meaning(objectiveValue) {
  return objectiveValue === "sum"
    ? "Quickest on average: the total travel time is as low as it can be."
    : "Fairest: the longest trip is as short as it can be.";
}

export function altText(c, objectiveValue) {
  const time = objectiveValue === "sum"
    ? `average trip ${mins(c.mean_time_min)}`
    : `longest trip ${mins(c.max_time_min)}`;
  const where = onStreet(c);
  return where ? `${where} · ${time}` : time;
}

// ── Stations and venues (a group with someone on public transport) ─
export const isStation = (c) => Boolean(c && c.kind === "station");
// The kinds of place a group can ask to meet at besides a station (#19), as the page says them.
export const VENUE_WORD = { pub: "pub", bar: "bar", cafe: "café", restaurant: "restaurant", park: "park" };
export const isVenue = (c) => Boolean(c && Object.hasOwn(VENUE_WORD, c.kind));
// A named place from the tables or OpenStreetMap, not a point on a street.
export const isPlace = (c) => isStation(c) || isVenue(c);
const placeName = (c) => (c && typeof c.name === "string" && c.name.trim() ? c.name.trim()
  : `this ${isVenue(c) ? VENUE_WORD[c.kind] : "station"}`);

// "a pub", "a café": every venue word takes "a".
const aKind = (c) => `a ${VENUE_WORD[c.kind]}`;

export function placeHeadline(best) {
  const where = isVenue(best) && best.name && best.name.trim() ? `${placeName(best)}, ${aKind(best)}` : placeName(best);
  return `Meet at ${where} (★ on the map). Everyone gets there within ${mins(best.max_time_min)}.`;
}

// Said under a venue answer: OpenStreetMap doesn't reliably say when a place is open.
export const OPEN_HOURS_HINT = "We don't know whether it's open.";

// A station or venue by its name; a street answer comes from the places the search checked,
// so it says so, and points at the Q marker when the street has no name.
export function quickestText(q) {
  const where = isPlace(q) ? `Quickest on average: ${placeName(q)}`
    : `Of the places we checked, the quickest on average is ${onStreet(q) || "at the Q on the map"}`;
  return `${where}. The average trip is ${mins(q.mean_time_min)}; ` +
    `the longest is ${mins(q.max_time_min)}.`;
}

export const TIE_TEXT = "About as good: the times can't tell these apart.";
export const ALL_TIES_TEXT = "These are all about as good as the first: the times can't tell them apart.";

// `markTie`: label a tie on its own row (off when every row is a tie and one sentence says so).
export function stationAltText(c, markTie = true) {
  return `${placeName(c)} · longest trip ${mins(c.max_time_min)}${markTie && c.tie ? " · about as good" : ""}`;
}

// What a "Plan journey" link does, said once under the answer.
export function journeyNote(windowLabel, place) {
  const when = typeof windowLabel === "string" && windowLabel ? `, not for ${windowLabel}` : "";
  return `"Plan journey" opens Google Maps with that person's start point and the ${isVenue(place) ? "place" : "station"}. ` +
    `It plans for now${when}.`;
}

// A journey planner link for one person to one place, in their way of travelling. Opened only
// when the person clicks it; the start and end points go to Google Maps then.
const TRAVELMODE = { walking: "walking", cycling: "bicycling", driving: "driving",
                     "driving-traffic": "driving", transit: "transit" };

export function journeyLink(person, place) {
  const at = (lat, lon) => `${Number(lat).toFixed(6)},${Number(lon).toFixed(6)}`;
  return "https://www.google.com/maps/dir/?api=1" +
    `&origin=${encodeURIComponent(at(person.lat, person.lon))}` +
    `&destination=${encodeURIComponent(at(place.lat, place.lon))}` +
    `&travelmode=${TRAVELMODE[person.mode] || "walking"}`;
}

// Notes the server adds when an answer is less good than usual. Strings only.
export function answerNotes(data) {
  return Array.isArray(data && data.notes) ? data.notes.filter((n) => typeof n === "string" && n) : [];
}

// "Ana", "Ana and Ben", "Ana, Ben and Cy".
export function joinNames(names) {
  if (names.length <= 1) return names[0] || "";
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

// The sentence for a "no meeting point" error, naming people from the group that was
// solved (`names` in the order they were sent). "" when the error doesn't say who or why,
// so the caller falls back to the server's own sentence.
export function noPlaceSentence(err, names) {
  if (err && (err.code === "outside_transit_area" || err.code === "no_transit_access")) {
    const who = (err.who || []).map((w) => names[w.index]).filter((n) => typeof n === "string" && n);
    if (!who.length) return "";
    const one = who.length === 1;
    if (err.code === "outside_transit_area") {
      return `Public transport times cover London only, and ${joinNames(who)} ${one ? "starts" : "start"} ` +
        `outside it. Pick another way for ${one ? "them" : "them all"} to travel.`;
    }
    return `${joinNames(who)} ${one ? "starts" : "start"} too far from a street or station in the public ` +
      `transport times. Move ${one ? "the pin" : "their pins"} onto a street, or pick another way to travel.`;
  }
  if (!err || err.code !== "no_feasible_meeting_point") return "";
  if (err.reason === "no_transit_time") {
    const who = (err.who || []).map((w) => names[w.index]).filter((n) => typeof n === "string" && n);
    if (!who.length) return "";
    const one = who.length === 1;
    return `From where ${joinNames(who)} ${one ? "starts" : "start"}, the public transport times have ` +
      `no trip within 3 hours to any station. Pick another way for ${one ? "them" : "them all"} to travel.`;
  }
  const who = (err.who || [])
    .map((w) => names[w.index])
    .filter((n) => typeof n === "string" && n);
  if (err.reason === "no_route" && who.length) {
    if (who.length === 1) {
      return `From where ${who[0]} starts, we couldn't find a route to any place we checked. ` +
        `Move ${who[0]}'s pin onto a street or path, or pick another way for them to travel.`;
    }
    return `From where ${joinNames(who)} start, we couldn't find a route to any place we checked. ` +
      "Move their pins onto a street or path, or pick other ways to travel.";
  }
  if (err.reason === "over_time_limit" && who.length) {
    const limits = who.length === 1 ? `${who[0]}'s time limit` : `the time limits of ${joinNames(who)}`;
    return `No place we checked is within ${limits}. Raise the limit or move people closer.`;
  }
  if (err.reason === "no_common_place") {
    // Who reaches fewer places than the best-connected person (cut off by water, say).
    const few = (err.who || []).filter((w) => w.reason === "reaches_few" &&
      typeof names[w.index] === "string" && names[w.index]);
    if (few.length === 1) {
      const w = few[0];
      const counted = Number.isInteger(w.reach) && Number.isInteger(w.of) && w.of > 0
        ? `${w.reach} of the ${w.of}` : "a few of the";
      return `${names[w.index]} can only get to ${counted} places we checked, and none of those ` +
        "work for everyone. Try a faster way for them to travel, or move someone closer.";
    }
    if (few.length > 1) {
      return `${joinNames(few.map((w) => names[w.index]))} can only get to a few of the places we ` +
        "checked, so no place works for everyone. Try faster ways for them to travel, or move people closer.";
    }
    return "There's no place we checked that everyone can get to. " +
      "Try moving people closer, or pick other ways to travel.";
  }
  return "";
}

// ── Share links (share.js) ──────────────────────────────────────
export const SHARE_COPIED = "Link copied. Opening it finds a place for this group.";
export const SHARE_FAILED = "Copy the link below.";
export const SHARE_BAD_LINK = "That link doesn't hold a group we can open.";
export const SHARE_RESTORED = "This is the group from the link you opened. Press Find to look for a place again.";
export const SHARE_WAITING = "Opening the shared group. Waiting for the public transport times…";
export const SHARE_LABEL = "a shared link";   // the row says "from a shared link"

// What changed between the link and this page, said before asking for Find.
export function shareChanged(walkers, windowGone, venueGone) {
  const parts = [];
  if (walkers.length) {
    parts.push(`public transport isn't available for ${joinNames(walkers)} here, so ` +
      `${walkers.length === 1 ? "they walk" : "they all walk"}`);
  }
  if (windowGone) parts.push("the time of day in the link isn't offered here");
  if (venueGone) parts.push("the kind of place in the link isn't offered here");
  if (!parts.length) return "";
  const said = parts.join("; ");
  return `This group changed from the link: ${said}. Press Find when it looks right.`;
}
