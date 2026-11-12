// The sentences the meetup page shows for an answer, and for "no place works".
// Pure functions, no page access, so they can be tested on their own (tests/js/words.test.js).

export const mins = (t) => Math.round(t);

// "on Baker Street", or "" when the place has no street name.
export function onStreet(c) {
  return c && typeof c.name === "string" && c.name.trim() ? `on ${c.name.trim()}` : "";
}

export function headline(best, objectiveValue) {
  const where = onStreet(best);
  const meet = where ? `Meet ${where} (★ on the map).` : "Meet here (★ on the map).";
  return objectiveValue === "sum"
    ? `${meet} The average trip is ${mins(best.mean_time_min)} min; the longest is ${mins(best.max_time_min)} min.`
    : `${meet} Everyone gets there within ${mins(best.max_time_min)} min.`;
}

export function meaning(objectiveValue) {
  return objectiveValue === "sum"
    ? "Quickest on average: the total travel time is as low as it can be."
    : "Fairest: the longest trip is as short as it can be.";
}

export function altText(c, objectiveValue) {
  const time = objectiveValue === "sum"
    ? `average trip ${mins(c.mean_time_min)} min`
    : `longest trip ${mins(c.max_time_min)} min`;
  const where = onStreet(c);
  return where ? `${where} · ${time}` : time;
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
  if (!err || err.code !== "no_feasible_meeting_point") return "";
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
    return "There's no place we checked that everyone can get to. " +
      "Try moving people closer, or pick other ways to travel.";
  }
  return "";
}
