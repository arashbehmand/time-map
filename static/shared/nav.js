// The switcher in every tool's header (#28): the other two tools, one tap away. The pages carry
// it as plain links; Whole city starts as plain text and becomes a link only once the server says
// its map can be shown here, so the switcher never leads to a page that can't draw. While it
// can't, the switcher says why in a few words, with the full sentence for screen readers.
import { cityOrKept } from "./offline.js";
import { cityShort, cityUnavailable } from "./tools.js";
import { pollStatus, statusLoading } from "./transit.js";

// status: the transit status (with its whole_city part), or null when it couldn't be read; then
// a Whole city kept for offline use (offline.js) still makes it a link.
export async function updateSwitcher(status, nav = document.querySelector("nav.switcher")) {
  if (!nav) return;
  status = await cityOrKept(status);
  const el = nav.querySelector('[data-tool="whole-city"]');
  if (!el || el.getAttribute("aria-current") === "page") return;
  const city = status && status.whole_city;
  if (city && city.available) {
    if (el.tagName === "A") return;
    const a = document.createElement("a");
    a.href = "/whole-city/";
    a.dataset.tool = "whole-city";
    a.textContent = "Whole city";
    el.replaceWith(a);
    return;
  }
  const reason = city ? city.reason : "";
  if (reason === "loading") return;             // watchCity asks again
  const span = document.createElement("span");
  span.dataset.tool = "whole-city";
  span.className = "off";
  span.title = cityUnavailable(reason);
  span.innerHTML = `Whole city <small aria-hidden="true">(${cityShort(reason)})</small>` +
    `<span class="visually-hidden">. ${span.title}</span>`;
  el.replaceWith(span);
}

// The tables may be ready before Whole city is (right after a deploy): keep asking for the
// switcher alone, without running the page's own status handling again.
export async function watchCity(status) {
  if (!statusLoading(status) || (status && status.loading)) return;
  updateSwitcher(await pollStatus({ tries: 20 }));
}
