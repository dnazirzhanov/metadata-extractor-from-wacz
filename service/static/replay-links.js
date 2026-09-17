// Injected by the replay service worker into every archived page, after wombat and before the
// page's own scripts, next to replay-guard.js (app.js names both on the worker URL).
//
// Why: a capture holds one page. A link inside it points at a page that capture does not hold, so
// following it inside the player can only fail - and on mandiner.hu it fails worse, because the
// site's Angular router turns the click into calls to its live API, which were never archived.
// Whether we hold the target at all is a question only the Causalia database can answer. This page
// runs on the same origin as the search app around the player, so the click is handed up to the
// app instead: it asks /resolve, then opens that article's own capture or says we do not have it.
//
// Registered on window in the capture phase, so it runs before any listener the page's own
// scripts put on the link - including the router's.
(() => {
  "use strict";
  const info = self.wbinfo || {};

  // Only when the search app is what surrounds the player; anywhere else, links behave as before.
  let app = null;
  try {
    if (window.top !== window && window.top.document.getElementById("replay-view")) app = window.top;
  } catch (_) {
    return;
  }
  if (!app) return;

  // wombat hands back a link's original URL, but a value it has not unwrapped is still the player's
  // form, on our own origin: /replay/w/<collection>/<timestamp>mp_/https://mandiner.hu/...
  // null when there is no original URL in it to follow.
  const original = (href) => {
    if (!href.startsWith(`${location.origin}/`)) return href;
    const at = href.indexOf("/http", location.origin.length);
    return at >= 0 ? href.slice(at + 1) : null;
  };
  const withoutFragment = (url) => url.split("#", 1)[0];

  window.addEventListener("click", (event) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.composedPath().find((node) => node instanceof HTMLAnchorElement || node instanceof HTMLAreaElement);
    if (!link || !link.hasAttribute("href") || link.hasAttribute("download")) return;
    const href = original(String(link.href));
    if (!href) return;
    let url;
    try {
      url = new URL(href, info.url);
    } catch (_) {
      return;
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") return;
    // The same page with another fragment (#comments) scrolls within the capture, as it did live.
    if (info.url && withoutFragment(url.href) === withoutFragment(info.url)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    app.postMessage({ type: "causalia:follow", url: url.href }, location.origin);
  }, true);
})();
