# The data contract

Every field the extractor writes, and why. Anything not listed here is not
written.

## `article.json`

```json
{
  "archive_id": "00003bdc9017bf448e12558c3b1848abb6b894c5bf733a07f609161ce5f47095",
  "outlet": "ripost.hu",
  "title": "Kegyetlenül meggyilkolták otthonában a híres színészt",
  "subtitle": "Kegyetlen gyilkosság áldozatává vált a 70 éves Thomas Jefferson Byrd.",
  "description": "Kegyetlen gyilkosság áldozatává vált a 70 éves Thomas Jefferson Byrd.",
  "author": [],
  "publisher": "Mediaworks Hungary Zrt.",
  "source_url": "https://ripost.hu/sztardzsusz/2020/10/kegyetlenul-...",
  "canonical_url": "https://ripost.hu/sztardzsusz/2020/10/kegyetlenul-...",
  "published_at": "2020-10-05T08:20:46.000Z",
  "updated_at": "2020-10-05T08:20:46.000Z",
  "captured_at": "2026-08-05T05:21:13.721Z",
  "language": "hu",
  "section": "sztardzsusz",
  "tags": ["Thomas Jefferson Byrd", "gyász", "megölték", "támadás"]
}
```

| Field | Notes |
| --- | --- |
| `archive_id` | `sha256(normalize_url(article URL))`. **The** identity of an article across the whole system — it is `urls.url_hash` in Postgres and the directory name on disk. Not a second identifier: verified to reproduce existing corpus directory names exactly. |
| `outlet` | Registrable domain. Taken from the corpus path when available, else derived from the page URL. |
| `source_url` / `canonical_url` | Both kept, deliberately. The crawled URL can redirect during capture, so they are not assumed equal. |
| `published_at` / `updated_at` | **Verbatim** as the page declared them. An ISO-8601 conversion that silently mangles a timezone is worse than the original string. |
| `captured_at` | From the capture's own `pages.jsonl`, not from the page. |
| `author` | A list. Empty is a *recorded absence*; a wrong byline is a fabricated claim about authorship, and this corpus is evidence. |
| `tags` | Only the article's own tag links. Scoped to an article-ish ancestor **and** rejected inside a container that is about other content — see below. |
| `language` | Normalised `hu-HU` → `hu`. All article text in this corpus is Hungarian; the English keys are our schema. |

### Tag scoping

The same `/cimke/` href pattern appears in three places that are not this
article's tags: the site header or trending strip, the hamburger menu, and the
tag chips printed on other articles' recommendation cards. Measured on this
corpus, 82–222 such links per page against 0–18 real ones. So an anchor is
admitted only if it reaches an article-scoped ancestor within 8 levels, and
rejected outright inside a `<nav>` element or a container matching
`menu|card|opinion|related|recommend|popular|sidebar|widget` — every one of those
describes a container that is about *other* content, whatever it sits inside.

`trending` was in that reject list and was **removed 2026-08-31**. It is the one
word describing prominence rather than ownership, and mandiner.hu uses it for the
article's own tag row (`div.trending-topics < man-trending-topics <
div.wrapper.with-aside < section.article-page`), so rejecting the word cost that
outlet its tags on every article. Its genuine site-wide strip is separated by the
scope test instead: that one sits in `div.header-hamburger-menu-left`, which
`menu` still rejects, and reaches no `article` ancestor.

Measured before the change, over 40 articles from 8 outlets: mandiner gained tags
on 5 of 5 (0 → 3–10, each set topical and different, so not a repeating site
strip — `Tapolca, MSZP, Jobbik` on a by-election piece, `Zrínyi Miklós,
Szigetvár, kazamata` on a castle piece); metropol, origo, ripost, magyarnemzet,
pestisracok, bama and heol were unchanged on 35 of 35. Do not put `trending` back
without re-running that comparison.

Two traps that must not be re-introduced: do **not** reject on the token
`header` — metropol and magyarnemzet keep real tags under `article-header`, so
the `<nav>` *element* is the safe structural signal — and do **not** reject on
`aside`, because origo's real tag block sits under
`div.wrapper.narrow-wrapper.with-aside`.

**Not written:** `site_name` (measured to hold the article title, not the site),
`capture_title` (duplicates `title`), `field_sources` (a debugging aid — logged,
not persisted), `url_hash` (`archive_id` is the same value).

## `content.json`

```json
{"blocks": [
  {"type": "paragraph", "index": 1,
   "xpath": "/html/body/article/div/p[1]", "text": "..."},
  {"type": "heading", "index": 2,
   "xpath": "/html/body/article/div/h2", "level": 2, "text": "..."},
  {"type": "image", "index": 3,
   "xpath": "/html/body/article/div/figure[1]/img", "image_id": "image_001"},
  {"type": "video", "index": 4,
   "xpath": "/html/body/article/div/figure[2]/video", "video_id": "video_001"},
  {"type": "quote", "index": 5,
   "xpath": "/html/body/article/div/blockquote", "text": "..."},
  {"type": "list", "index": 6, "xpath": "/html/body/article/div/ul",
   "items": [{"index": 1, "xpath": ".../ul/li[1]", "text": "..."}],
   "text": "..."}
]}
```

Always present: `type`, `index`, `xpath`. Textual types (`paragraph`,
`heading`, `quote`, `list`) add `text`; `heading` adds `level`; `image` adds
`image_id`; `video` adds `video_id`; `list` adds `items`, each item separately
addressable.

`index` is 1-based and contiguous in document order.

**There is no `block_id`.** The canonical reference is XPath → element in
`readability.html`; a hash of the block's text is not that.

`div`, `span`, `strong`, `em`, `a` and `br` are never blocks of their own —
inline markup stays inside the block that contains it, which is what makes
character offsets meaningful.

A **run of links with no prose around them** is furniture, not a paragraph, and
is not emitted as a block: at least two anchors, anchor text covering 80% or more
of the block, and no sentence-ending punctuation. That is a tag strip, a
breadcrumb or a related-article row. The test is deliberately narrow — a
paragraph that is one long link is ordinary writing, and so is a linked sentence
that ends in a full stop. Dropping one is logged, not warned: it is expected, and
warning would mark ordinary articles `partial`.

Measured on mandiner.hu `d1563b55`: the article's own tag row sits in
`div.trending-topics` *inside* `section.article-page`, so it survives furniture
stripping and Readability keeps it. It was being stored as a paragraph, which put
nine tag names into full-text search and let a citation point at them. Its nine
`/cimke/` anchors were also being recorded in `links.json` as article links;
without an owning block they are correctly absent.

`table`, `pre` and `dl` are not supported block types. They do not occur in a
1,400-block sample of this corpus; if one is encountered, its text is reported
as a warning and the extraction is downgraded to `partial` rather than dropping
it silently.

## `images.json`

```json
[{"id": "image_001", "filename": "images/image_001.jpg",
  "original_url": "https://cdn...", "caption": null, "alt": "...",
  "credit": null, "width": 1347, "height": 758,
  "mime_type": "image/jpeg", "image_available": true}]
```

`image_available: false` means the page referenced the image but the capture
does not hold its bytes; `filename` is then `null` and `original_url` is
preserved. The image is still marked in `readability.html` with
`data-archive-missing` and a `data-original-src` — never a live `src`, which
would make the offline page fetch from the publisher's CDN.

`id` is positional (`image_001` is the first image in document order), so it is
deterministic and re-derived on every run.

**Not written:** `position` (the block's `xpath` says where it is),
`block_id`, `size_bytes` (a property of the filesystem, not the article).

## `videos.json`

```json
[{"id": "video_001", "type": "youtube", "platform": "youtube",
  "external_id": "UwTYPHnSP8M",
  "url": "https://www.youtube.com/watch?v=UwTYPHnSP8M",
  "embed_url": "https://www.youtube.com/embed/UwTYPHnSP8M",
  "thumbnail_url": "https://i.ytimg.com/vi/UwTYPHnSP8M/hqdefault.jpg",
  "title": null, "caption": null, "local_file": null, "archived": false}]
```

`platform` and `external_id` are named to match the columns the project's
Postgres `videos` table already has. Thumbnail URLs are *recorded*, never
fetched.

`archived: true` with a `local_file` means the capture held playable bytes and
they were written. An HLS segment set is never written as a playable file — the
extractor has no ffmpeg, and reporting it as complete would make a later
backfill skip exactly the videos it needs to fetch.

**An adaptive stream is one video, not nine.** Facebook (and any DASH/HLS
source) serves a bitrate ladder: eight video rungs plus a separate audio track
for the same clip. None of them is playable alone — the video rungs are silent
and muxing needs ffmpeg, which this extractor does not have. So a ladder becomes
**one** record, with `archived: true` (the bytes really are in the capture) and
`local_file: null`, and a note in the log naming the encode tag.

Attribution is by the payload's own metadata first, the CDN host second. A
Facebook media URL carries a base64 `efg` parameter stating its `video_id`, which
is what tells three reels on one page apart; the host rule only decides when
exactly one embed on that platform is present. A payload neither step can place
gets its own record if it is a self-contained stream, and is **not recorded** if
it is one rung of a ladder — those bytes stay in the WACZ, which is the archive
of record.

Measured on mandiner.hu `f300764f`: three reels, 19 captured payloads. Before
this, `videos.json` claimed **22 videos** and 92 MB was written as 19 unplayable
files. After: **4 records** (3 reels plus one unattributable progressive stream)
and one 0.5 MB file — the only playable one. Verified against the 13-capture
test set with zero change to any other article.

**A video record may have no content block.** Players found on the page but
outside the article body are recorded (with corroboration — a recognised
platform, or video bytes held for that exact URL) but have no place in the
article's prose, so nothing in `content.json` refers to them. Every *block*
resolves to a record; not every record has a block.

**Not written:** `position`, `discovered_via`, `capture_urls`, `capture_bytes`,
`capture_complete`.

## `links.json`

```json
[{"url": "https://...", "text": "napirend", "context": "the owning block's text",
  "internal": false,
  "selector": {"type": "XPathSelector", "value": "/html/body/article/div/p[3]",
               "refinedBy": {"type": "TextPositionSelector", "start": 15, "end": 23},
               "quote": {"exact": "napirend", "prefix": "...", "suffix": "..."}}}]
```

The selector points at the **owning content block**, refined to the character
range the anchor text occupies inside it — not at the `<a>` element. A citation
needs to say where in the prose the link sits, and an offset into the paragraph
is what a highlighter can act on.

Only `http`/`https` links inside a content block are recorded. `rel` is not
stored: the sanitiser rewrites it on every anchor, so the value would be our own
echoed back.

`selector` is `null` in the rare case where the anchor text is not locatable in
its own block's normalised text; that is reported as a warning.

**Not written:** `position`, `block_id`.

## `extraction.json`

```json
{"extraction_version": "causalia-article-extractor/2.0.0",
 "extracted_at": "2026-08-31T14:00:59Z",
 "extraction_status": "success"}
```

`extraction_status` is `success`, `partial` or `failed`. `partial` means the
article was produced but something was recorded as imperfect (no blocks, a
missing image, no screenshot). `failed` means no artifacts were produced.

Counts, warnings, timings and per-phase statistics go to the **log**, not to
disk: they are facts about a run, not about an article, and persisting them made
every re-extraction a diff.

## `readability.html` — the canonical document

```
/html/body/article/header/h1              title      (metadata, not a block)
/html/body/article/header/p[@class=subtitle]         (metadata, not a block)
/html/body/article/div[@class=article-body]/…        every content block
/html/body/footer                                    provenance note
```

Every content block is a direct child of one container. The publisher's
structural nesting is flattened away, so the same paragraph gets the same XPath
whatever the CMS wrapped it in. Inline markup is preserved inside blocks.

Block-level elements are serialised with a newline between them. That newline is
a real text node, so `textContent` includes it — which is why a `<ul>`
normalises to `"first second"` rather than `"firstsecond"`, with no
special-casing and no divergence between our Python and the frontend's
JavaScript.

The document is self-contained: inline CSS, no scripts, no external requests,
`referrer: no-referrer`. Images resolve to `./images/`; third-party players are
rendered as links and never re-embedded, so an offline page never phones out.

## `original.html`

The captured markup with every network-fetching reference neutralised: scripts
and external stylesheets removed, `src`/`poster`/`data` moved to
`data-original-src`, `<base>` removed, inline event handlers stripped. It
renders unstyled — that is what "without depending on the host site's external
styling" means, and the styled rendering is preserved separately as the
screenshot.

It is an archival reference and **never** the coordinate system for selectors.

## Determinism

Two runs over the same archive produce byte-identical artifacts, except
`extracted_at`. Identifiers are positional (`image_001`, `video_001`), block
order is document order, and nothing is randomised or timestamped inside an id.
