# causalia-article-extractor

Turns one Browsertrix `page.wacz` into an article directory whose every content
reference is anchored to a canonical document.

```
readability.html      is the canonical document
XPath                 identifies the element
TextPositionSelector  identifies the exact character range
quote.exact           verifies the evidence
```

Everything downstream builds on that invariant.

This is a standalone package. It never imports the archiver, opens no socket and
no database connection, never crawls, never writes into the corpus, and never
deletes or modifies a `.wacz`.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Requires Python 3.10+ (milab2 runs 3.10.12). Five runtime dependencies, all of
which the first-generation extractor already runs in production: `warcio`,
`beautifulsoup4`, `lxml`, `readability-lxml`, `Pillow`.

## Use

```bash
causalia-extractor extract --input /path/to/page.wacz --output /path/to/out/
```

`--input` may be a single `.wacz`, an article directory, a shard, an outlet, or
a whole pages root; every archive beneath it is processed in a deterministic
order. It can read straight off the server filesystem:

```bash
causalia-extractor extract \
    --input /mnt/hdd/c0cshf/causalia/pages/bama.hu \
    --outlet bama.hu --limit 50 \
    --output /tmp/extraction-review/
```

| Flag | Meaning |
| --- | --- |
| `--input PATH` | archive or tree to read (default: `$CAUSALIA_PAGES_ROOT`) |
| `--output DIR` | where article directories are written (required) |
| `--outlet HOST` | restrict a tree walk to one outlet |
| `--limit N` | process at most N archives |
| `--copy-wacz` | also copy `page.wacz` into the output (off by default) |
| `--dry-run` | run everything, including the safety checks, write nothing |
| `--log-level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

Exit codes: `0` all extracted, `1` at least one failed, `2` an archive changed
underneath us or an unsafe artifact was refused, `130` interrupted.

## Output

```
<output>/<outlet>/<h2>/<archive_id>/
├── original.html      the captured markup, every network reference dead
├── readability.html   THE CANONICAL DOCUMENT
├── screenshot.png     Browsertrix's own capture, preferred over any fallback
├── article.json       cleaned metadata
├── content.json       semantic blocks, each with a validated XPath
├── images.json        + images/image_001.jpg
├── videos.json        + videos/video_001.mp4
├── links.json         each link with an XPath + offset + quote selector
└── extraction.json    extraction_version, extracted_at, extraction_status
```

`page.wacz` is not copied by default: the corpus is ~30 TB and copying would
double it. Pass `--copy-wacz` if you want it beside the artifacts.

See [docs/data-contract.md](docs/data-contract.md) for every field.

## The three mechanisms

### Text normalisation — `normalize.py`

One canonical function defines every string this system stores an offset into.
Text nodes are concatenated with **no separator**, exactly as a browser's
`textContent` does, and whitespace is collapsed afterwards.

```html
<p>Donald <strong>Trump</strong> announced
   <em>something</em>.</p>
```
becomes `Donald Trump announced something.`

This is not cosmetic. The first-generation extractor joined inline elements with
a space, so on the live corpus `több <strong>Spike Lee</strong>-filmben` was
stored as `több Spike Lee -filmben`. In Hungarian that hyphenated suffix is part
of the word, so the stored text was a *different string* from the page and every
character offset computed against it pointed somewhere else.

The frontend must be able to recompute this from the live DOM. The JavaScript
equivalent is exactly:

```js
el.textContent.replace(/[\u0009\u000a\u000b\u000c\u000d\u0020\u001c\u001d\u001e\u001f\u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+/gu, ' ').trim()
```

That character class is the union of Python's `\s` and JavaScript's `\s`, spelled
out because the two do not agree (Python matches U+001C–U+001F and U+0085;
JavaScript matches U+FEFF). No Unicode NFC/NFKC normalisation is applied —
NFKC changes string lengths relative to the live DOM and would silently move
every stored offset.

### XPath — `xpath.py`

Absolute positional paths, with a `[n]` predicate only where the element has
more than one same-tag sibling. That is `lxml`'s `getpath()` form and what a
browser's `document.evaluate()` resolves.

Paths are generated from a **re-parse of the exact bytes written to
`readability.html`**, never from the in-memory tree that produced it — HTML
serialisation can move nodes, and a path computed before serialisation may
describe a document nobody ever wrote. Every path is validated the moment it is
generated: it must select exactly one element, and that element must be the one
the path describes. A path that fails validation is never emitted.

### Selectors — `selectors.py`

```json
{
  "type": "XPathSelector",
  "value": "/html/body/article/div/p[3]",
  "refinedBy": { "type": "TextPositionSelector", "start": 10, "end": 38 },
  "quote": { "exact": "the exact referenced text",
             "prefix": "…32 chars before…", "suffix": "…32 chars after…" }
}
```

`[start, end)` — start inclusive, end exclusive, over the normalised text of the
element the XPath selects.

`quote.exact` is deliberate redundancy. Resolving walks XPath → element →
normalised text → `[start:end]` and compares with `quote.exact`; if they differ
the selector is **invalid** and must be reported as such. Highlighting a
different passage would be a fabricated citation.

This matters because positional XPath drifts. Measured on this corpus over 402
passages: after a single paragraph was inserted at the top of an article by an
ordinary extractor fix, only 43% of positional selectors still resolved to their
intended element — the other 57% resolved to the *wrong* element. The quote check
turns every one of those into a detected failure. `prefix`/`suffix` are the
repair path, letting a resolver re-find the passage by content; they are never
consulted while the XPath resolves and the quote matches.

## How a frontend resolves a citation

```js
const el = document.evaluate(sel.value, document, null,
    XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
if (!el) return "unresolvable";
const text = normalize(el);                       // the function above
const got = text.slice(sel.refinedBy.start, sel.refinedBy.end);
if (got !== sel.quote.exact) return "mismatch";   // never highlight anyway
// safe to scroll into view and highlight
```

## Tests

```bash
.venv/bin/python -m pytest -q
```

210 tests. No binary fixtures are committed — WACZ archives are built at test
time, because a real capture is 4–9 MB and would still not cover the cases that
matter most (a truncated zip, a capture with no HTML record, a redirect stub, a
206 video range).

`tests/test_integration.py` additionally runs the whole pipeline over real
archived captures and skips cleanly when they are absent. Point it at a
directory of real archives with `CAUSALIA_WACZ_TESTSET` (default
`~/causalia-wacz-testset`).

## Relationship to the existing system

Nothing in `causalia-final` is modified. The proven parts of the
first-generation extractor are **ported** here with attribution comments naming
the origin file — the WACZ reader, the allowlist sanitiser, the furniture
stripper, the ng-state fallback, the metadata candidate chains, the video
platform tables, the write fence. What is new is the canonical DOM, the
normaliser, XPath generation and validation, and the selector model.

PostgreSQL ingestion is deliberately **not** implemented. See
[docs/postgres-shape.md](docs/postgres-shape.md) for the shape the output is
designed to land in; the schema should be written against real extractor output,
not against assumptions about it.
