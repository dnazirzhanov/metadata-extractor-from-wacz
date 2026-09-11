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

## Production ingestion

The CLI above extracts a tree and is right for a handful of archives. A corpus
run needs a frontier that survives being interrupted, which is what `ingest/`
adds: the work ledger (`corpus.extraction_task`), a resumable extraction worker,
and a batched loader that refuses anything the quality contract rejected.

```bash
export CX_INGEST_DSN="host=... dbname=..."

# 1. queue an outlet. Excludes non-2xx captures and archives over 100 MB;
#    both get their own pass.
python -c "import os, psycopg2; from ingest import ledger; \
    print(ledger.seed(psycopg2.connect(os.environ['CX_INGEST_DSN']), \
    outlet='mandiner.hu', extractor_version='causalia-article-extractor/2.0.0'))"

# 2. extract. N processes against one ledger; the claim is what keeps them
#    disjoint. SIGTERM releases claims immediately rather than waiting.
python -m ingest.extract_worker --outlet mandiner.hu --output /path/to/out --limit 1000

# 3. load. Batched commits with a SAVEPOINT per article. RUN SEVERAL: ingestion
#    is round-trip-latency bound, not resource bound - a single loader leaves
#    the box 96% idle with Postgres at 0.4 of one core, and four measured
#    52.3 articles/s against 13.8. The claim keeps them disjoint.
for i in 1 2 3 4; do
    python -m ingest.load --outlet mandiner.hu --output /path/to/out --once &
done; wait

# 4. recovery, on cron: return tasks whose worker stopped beating, and sweep
#    orphaned temp files.
python -m ingest.reap --output /path/to/out
```

Three rules the ledger enforces, each of which used to be nobody's job:

* **An article is never searchable before it is ingested.** The loader claims
  only `success` / `partial_valid` extractions, so an unusable one has no row a
  query can reach; migration 025's CHECK stops a `failed` reading being current;
  migration 026's view is what `scripts/search.py` joins.
* **An interrupted run resumes.** The frontier is a query, not a filesystem walk.
* **A crashed worker's articles come back.** Liveness is a heartbeat, never the
  age of a row — and a failure is retryable until `attempts` runs out, after
  which the task is `quarantined` and reported rather than retried forever.

`extraction.json` is the commit marker: written last, after every other
artifact, and carrying the manifest of what it committed. The loader accepts a
directory only if the marker is there, its quality is usable, and every artifact
it promises exists.

### Screenshots, and running without them

Screenshots are never removed from the architecture - they are switched off for
a run and backfilled from the same archives afterwards. Measured on milab2:

| | archives/s @8 workers | @20 workers | MB/article |
| --- | --- | --- | --- |
| `--stages content,screenshot` | 6.95 | 11.13 | 5.33 |
| `--stages content` | 7.52 | 16.73 | ~0.50 |
| `screenshot_worker` (backfill) | — | 39.99 | 4.79 |

The gain from deferring is a WRITE-BANDWIDTH effect, so it only appears at
concurrency high enough to saturate the disk: 1.08x at 8 workers, 1.50x at 20,
where `Dirty` sits at the kernel's 10%-of-RAM writeback threshold. The backfill
is cheap because `read_archive(html_only=True)` skips buffering the image and
video bodies, not just parsing them.

```bash
python -m ingest.extract_worker --outlet mandiner.hu --output /path/to/out --stages content
# ... later, from the same archives, creating no new reading of the article:
python -m ingest.screenshot_worker --outlet mandiner.hu --output /path/to/out
```

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

## Demonstrating the search engine

[`MENTOR_DEMO.sql`](MENTOR_DEMO.sql) is a runnable walkthrough of every search
capability, in a ten-minute order, written to be executed statement by statement
from a SQL console. Every query in it was executed against the evaluation
database before it was written down, and the counts in its comments are the
counts it returned — including the two limitations it deliberately shows rather
than hides.

The database listens on loopback on milab2, so open a tunnel first:

```bash
ssh -N -L 55435:127.0.0.1:55435 "$MILAB2"
```

`$MILAB2` is the archiver host's `user@host`. It is deliberately not written
down in this repository — see the deployment notes — for the same reason
`migrations/README.md` and `scripts/migrate.sh` use the variable.

Then point a PostgreSQL data source at `127.0.0.1:55435`, database
`causalia_eval`, user `causalia`, password `eval`. In IntelliJ or DataGrip open
the file against that data source and run one statement at a time with
`Ctrl/Cmd+Enter` — the demo reads as a narrative and the comments say what to
point out at each step.

The same file runs unchanged against `causalia_d1` on port 55440 (the 16,008
article random sample from the D1 staged ingest), where the counts are roughly
14× larger. Use `causalia_eval` for a demonstration: its numbers are the ones in
the comments, and it is the corpus every published figure is measured against.
