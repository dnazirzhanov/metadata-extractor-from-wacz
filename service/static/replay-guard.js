// Injected by the replay service worker into every archived page, after wombat and before the
// page's own scripts (app.js asks for it through the worker's injectScripts parameter).
//
// Why: mandiner.hu's own bundle (chunk-LM4E6RYD.js in the August 2026 captures, 40 of 40 sampled)
// adds Ad-Shield's loader, <script src="https://html-load.com/core.js">, whose onload/onerror
// handlers treat any failure to load it as an ad blocker. It was never archived, so in replay it
// always fails: the handlers empty <body> every 100 ms and cover the article with a full-screen
// error-report.com frame, which the player shows as "Archived Page Not Found".
//
// A script element that never gets a src neither loads nor errors, so neither handler runs. The
// archive itself is untouched; only this one third-party loader is kept from starting.
(() => {
  "use strict";
  // Matches the original URL and the player's rewritten form (.../js_/https://html-load.com/...).
  const LOADER = /\/\/(?:[^/?#]+\.)?html-load\.com(?:[/?#:]|$)/i;
  const isLoader = (el, value) => el instanceof HTMLScriptElement && LOADER.test(String(value));
  const skip = (value) => console.info("Causalia replay: not starting the anti-adblock loader", String(value));

  const setAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    if (String(name).toLowerCase() === "src" && isLoader(this, value)) return skip(value);
    return setAttribute.call(this, name, value);
  };

  const src = Object.getOwnPropertyDescriptor(HTMLScriptElement.prototype, "src");
  if (src && src.set && src.configurable) {
    Object.defineProperty(HTMLScriptElement.prototype, "src", {
      ...src,
      set(value) {
        if (isLoader(this, value)) return skip(value);
        src.set.call(this, value);
      },
    });
  }
})();
