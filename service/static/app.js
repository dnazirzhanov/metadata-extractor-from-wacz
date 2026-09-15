"use strict";

const PAGE_SIZE = 20;
const REASONS = {
  metadata: "title / metadata",
  body: "in the text",
  both: "title + text",
  caption: "image caption",
  document: "terms spread across the article",
};

// The replay service worker, told to inject static/replay-guard.js into every archived page (that
// file says why). The player appends "?serveIndex=1" to this name itself - the embed and its frame
// both do - so the trailing "&_=" swallows that second "?": both register this same worker URL, and
// serveIndex still reaches the worker. Injected paths must come in on the worker URL; the worker
// refuses to fetch ones passed only through the embed's config.
const REPLAY_WORKER = "sw.js?serveIndex=1&injectScripts=/static/replay-guard.js&_=";

const $ = (id) => document.getElementById(id);
const cache = new Map();   // "q|phrase|from|to|page" -> results, so Back is instant
let inflight = null;       // AbortController of the running request
let tick = null;           // elapsed-time interval
let cameFromResults = false;
let replayUI = null;       // promise for /replay/ui.js, loaded on first replay

// Publication dates as the date inputs and the API write them; anything else is ignored.
const day = (v) => (/^\d{4}-\d{2}-\d{2}$/.test(v || "") ? v : null);
const ordered = (from, to) => (from && to && from > to ? { from: to, to: from } : { from, to });

function readState() {
  const p = new URLSearchParams(location.search);
  return {
    q: (p.get("q") || "").trim(),
    phrase: p.get("phrase") === "1",
    page: Math.max(1, parseInt(p.get("page") || "1", 10) || 1),
    article: p.get("article"),
    ...ordered(day(p.get("from")), day(p.get("to"))),
  };
}

function urlFor(s) {
  const p = new URLSearchParams();
  if (s.q) p.set("q", s.q);
  if (s.phrase) p.set("phrase", "1");
  if (s.from) p.set("from", s.from);
  if (s.to) p.set("to", s.to);
  if (s.page > 1) p.set("page", String(s.page));
  if (s.article) p.set("article", String(s.article));
  const query = p.toString();
  return query ? `/?${query}` : "/";
}

function navigate(s) {
  history.pushState(null, "", urlFor(s));
  render();
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

const fmtInt = (n) => new Intl.NumberFormat("en").format(n);
const fmtDate = (iso) => (iso ? iso.slice(0, 10) : "");
const fmtBytes = (n) => `${(n / 1048576).toFixed(1)} MB`;

function setStatus(node, text, isError = false) {
  node.textContent = text;
  node.classList.toggle("error", isError);
}

function startTimer(label) {
  stopTimer();
  const t0 = performance.now();
  const update = () => setStatus($("status"), `${label}… ${Math.round((performance.now() - t0) / 1000)} s`);
  update();
  tick = setInterval(update, 1000);
  return t0;
}

function stopTimer() {
  clearInterval(tick);
  tick = null;
}

function cancelInflight() {
  if (inflight) inflight.abort();
  inflight = null;
  stopTimer();
}

async function fetchJSON(url, signal, what) {
  const r = await fetch(url, { signal });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    const detail = typeof body.detail === "string" ? body.detail : `HTTP ${r.status}`;
    throw new Error(`${what}: ${detail}`);
  }
  return body;
}

// ts_headline marks matches as «…»; everything else is archived text and stays text.
function snippet(text) {
  const p = el("p", "card-snippet");
  let buf = "";
  let marked = false;
  const flush = () => {
    if (buf) p.append(marked ? el("mark", null, buf) : document.createTextNode(buf));
    buf = "";
  };
  for (const ch of String(text)) {
    if (ch === "«" && !marked) { flush(); marked = true; }
    else if (ch === "»" && marked) { flush(); marked = false; }
    else buf += ch;
  }
  flush();
  return p;
}

function card(hit, s) {
  const a = hit.article;
  const li = el("li", "card");
  const link = el("a", "card-link");
  link.href = urlFor({ ...s, article: a.id });
  link.addEventListener("click", (e) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    cameFromResults = true;
    navigate({ ...s, article: a.id });
  });
  link.append(
    el("h2", "card-title", a.title || "(untitled)"),
    el("p", "card-meta", [a.outlet, fmtDate(a.published_at), a.section].filter(Boolean).join(" · ")),
  );
  if (a.authors.length) link.append(el("p", "card-authors", a.authors.join(", ")));
  const passage = hit.passages[0];
  if (passage && passage.highlight) link.append(snippet(passage.highlight));
  else if (passage) link.append(el("p", "card-snippet", passage.text));
  if (hit.match_reason) link.append(el("span", "chip", REASONS[hit.match_reason] || hit.match_reason));
  li.append(link);
  return li;
}

// "published 2024-03-01 – 2024-03-31", "published on 2024-03-15", or "" when no date is set.
function rangeText(s) {
  if (s.from && s.to) return s.from === s.to ? `published on ${s.from}` : `published ${s.from} – ${s.to}`;
  if (s.from) return `published from ${s.from}`;
  if (s.to) return `published up to ${s.to}`;
  return "";
}

function drawResults(s, body, seconds) {
  const total = body.total_articles;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  $("results").replaceChildren(...body.hits.map((hit) => card(hit, s)));
  const took = seconds === undefined ? "" : ` · ${seconds.toFixed(1)} s`;
  const range = rangeText(s) ? ` ${rangeText(s)}` : "";
  if (total === 0) setStatus($("status"), s.q ? `No articles match “${s.q}”${range}${took}` : `No articles${range}${took}`);
  else if (!body.hits.length) setStatus($("status"), `${fmtInt(total)} articles${range} · page ${s.page} is past the last page`);
  else setStatus($("status"), `${fmtInt(total)} article${total === 1 ? "" : "s"}${range} · page ${s.page} of ${fmtInt(pages)}${took}`);
  $("pager").hidden = pages <= 1 && s.page <= 1;
  $("prev").disabled = s.page <= 1;
  $("next").disabled = s.page >= pages;
  $("page-info").textContent = `${s.page} / ${fmtInt(pages)}`;
}

// With words, /search takes the dates as filters. Without words, /articles lists the date range; its
// rows are put in /search's shape so the cards and pager are shared (no passage, no match reason).
async function fetchPage(s, signal) {
  const params = new URLSearchParams({ limit: PAGE_SIZE, offset: (s.page - 1) * PAGE_SIZE });
  if (s.from) params.set("from", s.from);
  if (s.to) params.set("to", s.to);
  if (s.q) {
    params.set("q", s.q);
    params.set("phrase", s.phrase);
    params.set("passages_per_article", 1);
    return fetchJSON(`/search?${params}`, signal, "Search failed");
  }
  const list = await fetchJSON(`/articles?${params}`, signal, "Listing failed");
  return {
    total_articles: list.total_articles,
    hits: list.articles.map((article) => ({ article, passages: [], match_reason: null })),
  };
}

async function showResults(s) {
  cancelInflight();
  $("player").replaceChildren();
  $("replay-view").hidden = true;
  $("search-view").hidden = false;
  const label = [s.q, rangeText(s)].filter(Boolean).join(" · ");
  document.title = label ? `${label} · Causalia search` : "Causalia search";
  $("results").replaceChildren();
  $("pager").hidden = true;
  if (!s.q && !s.from && !s.to) {
    setStatus($("status"), "");
    $("q").focus();
    return;
  }
  const key = `${s.q}|${s.phrase ? 1 : 0}|${s.from || ""}|${s.to || ""}|${s.page}`;
  if (cache.has(key)) {
    drawResults(s, cache.get(key));
    return;
  }
  const ctrl = new AbortController();
  inflight = ctrl;
  const t0 = startTimer(s.q ? "Searching" : "Listing");
  try {
    const body = await fetchPage(s, ctrl.signal);
    cache.set(key, body);
    if (inflight !== ctrl) return;
    stopTimer();
    drawResults(s, body, (performance.now() - t0) / 1000);
  } catch (err) {
    if (err.name === "AbortError" || inflight !== ctrl) return;
    stopTimer();
    setStatus($("status"), err.message, true);
  } finally {
    if (inflight === ctrl) inflight = null;
  }
}

function loadReplayUI() {
  replayUI ??= new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "/replay/ui.js";
    script.onload = resolve;
    script.onerror = () => {
      replayUI = null;
      reject(new Error("The replay player (/replay/ui.js) could not be loaded"));
    };
    document.head.append(script);
  });
  return replayUI;
}

async function showReplay(s) {
  cancelInflight();
  $("search-view").hidden = true;
  $("replay-view").hidden = false;
  $("back").href = urlFor({ ...s, article: null });
  $("player").replaceChildren();
  $("replay-title").textContent = "";
  $("replay-sub").textContent = "";
  $("original").removeAttribute("href");
  $("download").removeAttribute("href");
  if (!/^\d+$/.test(s.article)) {
    setStatus($("replay-status"), "That is not an article id.", true);
    return;
  }
  setStatus($("replay-status"), "Loading the archive…");
  const ctrl = new AbortController();
  inflight = ctrl;
  try {
    const [info] = await Promise.all([
      fetchJSON(`/articles/${s.article}/replay`, ctrl.signal, "Could not open the article"),
      loadReplayUI(),
    ]);
    if (inflight !== ctrl) return;
    const a = info.article;
    const title = a.title || "(untitled)";
    document.title = `${title} · archived`;
    $("replay-title").textContent = title;
    $("replay-sub").textContent = [
      a.outlet,
      info.captured_at ? `captured ${info.captured_at.slice(0, 16).replace("T", " ")} UTC` : "",
      fmtBytes(info.wacz_bytes),
    ].filter(Boolean).join(" · ");
    $("original").href = info.page_url;
    $("download").href = info.wacz_url;
    $("download").setAttribute("download", `${a.url_hash}.wacz`);
    const player = document.createElement("replay-web-page");
    player.setAttribute("source", info.wacz_url);
    player.setAttribute("url", info.page_url);
    if (info.ts) player.setAttribute("ts", info.ts);
    player.setAttribute("replaybase", "/replay/");
    player.setAttribute("embed", "replayonly");
    player.setAttribute("swName", REPLAY_WORKER);
    $("player").append(player);
    setStatus($("replay-status"), "The first replay can take 10–20 s while the player starts.");
  } catch (err) {
    if (err.name === "AbortError" || inflight !== ctrl) return;
    setStatus($("replay-status"), err.message, true);
  } finally {
    if (inflight === ctrl) inflight = null;
  }
}

function render() {
  const s = readState();
  $("q").value = s.q;
  $("phrase").checked = s.phrase;
  $("from").value = s.from || "";
  $("to").value = s.to || "";
  if (s.article) showReplay(s);
  else showResults(s);
}

window.addEventListener("popstate", render);

document.addEventListener("DOMContentLoaded", () => {
  $("search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("q").value.trim();
    const { from, to } = ordered(day($("from").value), day($("to").value));
    if (q || from || to) navigate({ q, phrase: $("phrase").checked, from, to, page: 1 });
    else $("q").focus();
  });
  $("prev").addEventListener("click", () => {
    const s = readState();
    navigate({ ...s, article: null, page: s.page - 1 });
  });
  $("next").addEventListener("click", () => {
    const s = readState();
    navigate({ ...s, article: null, page: s.page + 1 });
  });
  $("back").addEventListener("click", (e) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    if (cameFromResults) history.back();
    else navigate({ ...readState(), article: null });
  });
  render();
});
