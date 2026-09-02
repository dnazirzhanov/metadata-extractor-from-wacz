# PostgreSQL schema design — Causalia article corpus

**Status: design for review. No migration, no models, no ingestion code.**
Supersedes the non-binding sketch in [postgres-shape.md](postgres-shape.md).

## How this was derived

Not from the field lists in the brief — several of those are stale. Every field
below was read out of the extractor's actual output: 13 article directories
(`scratchpad/final`), profiled for key presence, null rate and type, plus the
extractor source for what is *guaranteed* rather than merely true of the sample.

Measured against the live database (Postgres 16.14) rather than assumed:

| Fact | Value |
| --- | --- |
| Existing tables | `public.{urls, archives, videos, schema_migrations}` — only `public` exists |
| `urls` rows | 6,032,673 |
| `archives` rows, `status='success'` | 4,215,169 |
| `hungarian` text-search config | **present and stems correctly** (see §D) |
| `unaccent`, `pg_trgm` | available, **not installed** |
| `vector` (pgvector) | **not available** on this image |

Two consequences to note immediately:

* **`videos` is already taken** by the yt-dlp backfill table. The article-video
  table cannot be called `videos`.
* Everything new goes in a **new `corpus` schema**, not `public`. See §H.

## 0. The verified output contract

Corrections to the brief, from the actual artifacts:

| The brief lists | Reality |
| --- | --- |
| images `size_bytes` | not emitted — removed as a filesystem property |
| videos `discovered_via`, `capture_urls`, `capture_bytes`, `capture_complete` | not emitted |
| links `position`, `block_id` | not emitted |
| article `site_name`, `capture_title`, `field_sources`, `url_hash` | not emitted |
| extraction `status`, `extractor`, `source`, `screenshot`, `results`, `warnings`, `notes`, `timings_ms` | not emitted; the file is exactly three keys |

Observed null rates over 13 articles (sample is deliberately media-biased, so
treat rates as existence proofs, not frequencies):

```
article.json     15 keys, all always present
                 subtitle 31% null
images.json      credit 100% null   caption 65%   alt 26%   width/height 13%
videos.json      title 100% null    caption 100%  thumbnail_url 75%
                 url 50%            external_id 33%  local_file 67%  platform 8%
links.json       context 35% null   selector 0% null (but CAN be null - tested)
                 (measured pre-fix; that 35% WAS the synthetic embed anchors of
                  §H.2, so on post-fix output context is null far more rarely)
content blocks   paragraph 125 {type,index,xpath,text}
                 heading    18 {type,index,xpath,level,text}
                 image      25 {type,index,xpath,image_id}
                 video       9 {type,index,xpath,video_id}
                 list        2 {type,index,xpath,items,text}
```

Nullability the *code* guarantees beyond the sample: `title` and `published_at`
can be null (each raises a warning), `captured_at` is null when the capture has
no `pages.jsonl`, `images.filename` is null when `image_available` is false, and
`links.selector` is null when the anchor text is not locatable in its own block.

Sizing, projected from the sample onto 4,215,169 successful archives:

```
content_block rows   ~58 M
body text            ~11.7 GB
images               ~7.6 M      videos ~3.8 M (media-biased)   links ~6.3 M
```

---

## A. Entity model

### A.1 The identity question, and a finding

**`archive_id` in the extractor output is not an archive identifier.** It is
`sha256(normalize_url(article URL))` — byte-identical to `urls.url_hash`,
verified against the live corpus on four articles. It identifies *the article
URL*, and there is exactly one per corpus directory.

The capture layer already exists and is authoritative: `archives` holds **many
rows per URL** with `wacz_sha256` as the evidence anchor. So the three concepts
the brief asks us to separate are already separate in the data, just misnamed in
the JSON:

```
urls.url_hash        the ARTICLE     one per URL          ← extractor's "archive_id"
archives.id          the CAPTURE     many per URL         ← the .wacz
(new) extraction     the READING     many per capture     ← extractor version N
```

Recommendation, and the name should be fixed in the extractor later:
`archive_id` should be read as `url_hash` and the column called what it is.

### A.2 Where each thing lives, and why

The governing question is *what may be replaced by a re-extraction, and what
must survive it*.

| Layer | Replaced on re-extraction? | Therefore |
| --- | --- | --- |
| Article identity (`url_hash`) | never | stable external key |
| Article metadata (title, tags) | corrected in place | columns on `article`, UPSERT |
| Content blocks, images, videos, links | wholly regenerated | owned by an **extraction** |
| Citations to passages | must survive | **their own table**, self-contained |

The last row is the important one. Content blocks carry positional `index` and
positional `xpath`, both of which provably drift — the lead-paragraph fix shifted
every index in the corpus, and this session's own link-strip fix moved
`p[9]→p[8]`. So **a citation must not be a foreign key to a content block.** A
citation stores the full selector as a value; the block reference is a
convenience pointer that is allowed to go stale.

This is why the measured numbers matter: positional XPath alone survived 43% of a
single paragraph insertion, XPath plus textual context 99%. The schema has to
store the thing that survives.

### A.3 Entities

```
corpus.article               one row per article URL. The searchable, citable unit.
corpus.article_extraction    one run of the extractor over one capture.
corpus.content_block         ordered semantic blocks. Owned by an extraction.
corpus.article_image         image metadata + local file. Owned by an extraction.
corpus.article_video         video metadata + local file. Owned by an extraction.
corpus.article_link          outbound/internal links with their selector.
corpus.article_artifact      document-level files (readability.html, screenshot…).
corpus.passage_reference     a citation to an exact passage. Survives re-extraction.
```

Eight tables. Deliberately absent, with reasons, in §B.9.

---

## B. Tables

Conventions: `bigint` identity PKs for high-volume internal rows (8 bytes, insert
locality); `text` for all strings (Postgres `varchar(n)` buys nothing);
`timestamptz` never `timestamp`; child rows `ON DELETE CASCADE` from their owner.

### B.1 `corpus.article`

One row per article URL. This is what an agent means by "an article".

| Column | Type | Null | Default | Key | Reason |
| --- | --- | --- | --- | --- | --- |
| `id` | `bigint` | no | identity | **PK** | internal joins; 58M child rows point here |
| `url_hash` | `text` | no | | **UNIQUE**, FK→`urls(url_hash)` **ON DELETE RESTRICT** | the article's identity, and the **external key Neo4j should use** — deterministic from the URL, so a graph node can be created without a Postgres round-trip. See §B.1.1 |
| `outlet` | `text` | no | | index (composite) | "articles by outlet" is the commonest browse |
| `source_url` | `text` | no | | | the URL that was crawled |
| `canonical_url` | `text` | yes | | index | what the page declared; **kept separately because captures redirect**. Non-unique: several source URLs can share one canonical |
| `title` | `text` | yes | | in search vector | nullable — the extractor warns and continues |
| `subtitle` | `text` | yes | | in search vector | the standfirst; 31% null |
| `description` | `text` | yes | | in search vector | |
| `authors` | `text[]` | no | `'{}'` | GIN | see §B.8 |
| `publisher` | `text` | yes | | | the org as the page declares it |
| `section` | `text` | yes | | | first URL segment / declared section |
| `language` | `text` | yes | | | `hu` throughout; kept for the day it isn't |
| `tags` | `text[]` | no | `'{}'` | GIN | see §B.8 |
| `published_at` | `timestamptz` | yes | | index (composite) | nullable; the extractor warns |
| `updated_at_source` | `timestamptz` | yes | | | the page's own `dateModified`. Named to not collide with our row bookkeeping |
| `captured_at` | `timestamptz` | yes | | | when Browsertrix fetched it; null with no `pages.jsonl` |
| `published_at_raw` | `text` | yes | | | **the verbatim string.** The extractor keeps dates verbatim on purpose: "an ISO-8601 conversion that silently mangles a timezone is worse than the original string". Parse into `published_at` for querying, keep the original for evidence |
| `current_extraction_id` | `bigint` | yes | | FK→`article_extraction(id)` | which reading is live. Nullable to break the circular FK at insert |
| `search_tsv` | `tsvector` | no | generated stored | GIN | see §D |
| `first_ingested_at` | `timestamptz` | no | `now()` | | |
| `row_updated_at` | `timestamptz` | no | `now()` | | touched by ingestion |

#### B.1.1 The foreign key to `urls` — decided

**`corpus.article.url_hash` REFERENCES `urls(url_hash)` ON DELETE RESTRICT.**

Verified before committing to it: all 13 extracted articles' `archive_id` values
resolve to existing `urls` rows — **0 would violate the constraint**. That
includes both pilot captures, whose identity was derived from the page URL rather
than from a corpus path, which is the case most likely to have produced an
orphan. It did not.

```sql
WITH ids(h) AS (VALUES (…13 archive_ids…))
SELECT count(*) AS extracted, count(u.url_hash) AS present_in_urls,
       count(*) - count(u.url_hash) AS would_violate_fk
FROM ids LEFT JOIN urls u ON u.url_hash = ids.h;
--  extracted | present_in_urls | would_violate_fk
--         13 |              13 |                0
```

**Why `RESTRICT` and not `CASCADE`.** The two existing tables that reference
`urls` (`archives`, `videos`) both use `ON DELETE CASCADE`, and copying that would
be a mistake. An archive row is *derived* data: losing it with its URL is
harmless and re-creatable. `corpus.article` is the root of a citation graph — a
`urls` prune under CASCADE would silently delete `passage_reference` rows, which
is evidence loss, exactly the failure this whole design exists to prevent.

The cost of `RESTRICT` is currently zero: **nothing in the crawler, the scripts or
the migrations ever deletes from `urls`.** Quarantine flags a row
(`quarantine_reason`, `exhausted_at`) and never removes it. So `RESTRICT` changes
no behaviour today and converts a hypothetical future prune from silent evidence
loss into a loud error that forces a deliberate decision.

**Operational consequence, stated plainly.** An article cannot be ingested before
its `urls` row exists. In the normal pipeline that is always satisfied — the
capture only exists because the crawler claimed a `urls` row. The one case it
bites is a `.wacz` handed to us from outside the pipeline for a URL never
collected: ingestion will fail on the constraint. The fix is to collect the URL
first, which is the correct outcome — the corpus should know about every URL it
holds, and a silent orphan article would be worse.

No extra index is needed for the constraint in either direction: the referenced
side is `urls`' primary key, and the referencing side is `UNIQUE`.

### B.2 `corpus.article_extraction`

One row per extractor run. This is where extraction lifecycle lives — **not on
`article`**, because "which version produced this" is a property of a run, and
because a new run must be insertable before it becomes current.

| Column | Type | Null | Default | Key | Reason |
| --- | --- | --- | --- | --- | --- |
| `id` | `bigint` | no | identity | **PK** | |
| `article_id` | `bigint` | no | | FK→`article` CASCADE, index | |
| `extractor_version` | `text` | no | | | `causalia-article-extractor/2.0.0` verbatim |
| `extraction_status` | `text` | no | | CHECK in (`success`,`partial`,`failed`) | the only lifecycle state the extractor persists |
| `extracted_at` | `timestamptz` | no | | | |
| `wacz_sha256` | `text` | yes | | index | **which capture was read.** Ties the reading to `archives.wacz_sha256`, so "re-extract everything read from a capture we later replaced" is a query. The extractor does not currently emit this — see §Open |
| `archive_row_id` | `bigint` | yes | | FK→`archives(id)` | optional precise link to the capture record |
| `is_current` | `boolean` | no | `false` | **partial UNIQUE** `(article_id) WHERE is_current` | exactly one live reading per article, enforced by the database rather than by hope |

`extraction_status` is *not* denormalized onto `article`. A partial extraction is
a property of the reading; if you want "articles whose current reading is
partial", that is a join on `current_extraction_id`, which is a PK lookup.

### B.3 `corpus.content_block`

The searchable, citable body of the article. ~58M rows.

| Column | Type | Null | Default | Key | Reason |
| --- | --- | --- | --- | --- | --- |
| `id` | `bigint` | no | identity | **PK** | a convenience handle, *not* a citation anchor (§E) |
| `extraction_id` | `bigint` | no | | FK→`article_extraction` CASCADE | content belongs to a reading |
| `article_id` | `bigint` | no | | FK→`article` CASCADE, index (composite) | denormalized: every content query filters by article, and going via extraction would make the hot path a two-hop join for no benefit |
| `block_index` | `int` | no | | UNIQUE `(extraction_id, block_index)` | **keep it** — see below |
| `block_type` | `text` | no | | CHECK in (`paragraph`,`heading`,`image`,`video`,`quote`,`list`) | a closed set the extractor guarantees; a CHECK catches a schema/extractor drift immediately |
| `xpath` | `text` | no | | | the address in `readability.html` |
| `text` | `text` | yes | | | null for `image`/`video` blocks, which carry no prose |
| `heading_level` | `smallint` | yes | | CHECK 2–6 | only for `heading` |
| `image_id` | `bigint` | yes | | FK→`article_image` | only for `image` |
| `video_id` | `bigint` | yes | | FK→`article_video` | only for `video` |
| `text_tsv` | `tsvector` | yes | generated stored | GIN | see §D |

**Should `index` be retained?** Yes, as `block_index`, for three reasons that are
not "the JSON has it":

1. It *is* the article's reading order. `get_article_content` must return blocks
   in document order, and ordering by `xpath` is wrong — `p[10]` sorts before
   `p[2]` as text, and image blocks interleave.
2. It makes a real integrity check possible: `UNIQUE (extraction_id,
   block_index)` plus a contiguity assertion at ingestion catches a truncated
   extraction that would otherwise look like a short article.
3. It is 4 bytes.

But it is explicitly **not** identity. `(article_id, block_index)` must never be
stored as a citation, because an extractor improvement shifts it — measured
twice. The schema encodes that by putting `block_index` on a row owned by an
*extraction*: it cannot be mistaken for a durable article-level coordinate.

Lists: the extractor emits `items[]` with a per-item `xpath` and `text`. **Model
them as ordinary `content_block` rows** with `block_type='list'` for the `<ul>`
and — a decision to confirm — nothing per item. The block's own `text` already
holds the items joined by whitespace, so the list is searchable and citable at
block level. A separate `content_block_item` table would add a table and a join
to serve one hypothetical query ("cite list item 2 specifically"), and only 2 of
179 blocks in the sample are lists. Recommendation: skip it, revisit if list
citations are ever actually requested. The item xpaths remain recoverable from
`readability.html`.

### B.4 `corpus.article_image`

| Column | Type | Null | Key | Reason |
| --- | --- | --- | --- | --- |
| `id` | `bigint` | no | **PK** | |
| `article_id` | `bigint` | no | FK CASCADE, index | `get_article_images` |
| `extraction_id` | `bigint` | no | FK CASCADE | replaced with its reading |
| `local_ref` | `text` | no | UNIQUE `(extraction_id, local_ref)` | `image_001` — the extractor's positional handle, the join key at ingestion time, and what `content.json` refers to |
| `file_path` | `text` | yes | | relative path, null when unavailable |
| `original_url` | `text` | yes | | where it came from; needed to re-fetch or to attribute |
| `media_type` | `text` | yes | | serve it with the right Content-Type |
| `width`, `height` | `int` | yes | | layout without opening the file; 13% null |
| `alt` | `text` | yes | | accessibility + weak caption signal |
| `caption` | `text` | yes | | real editorial text; 65% null |
| `credit` | `text` | yes | | **kept but unproven** — 100% null across the sample. It is one nullable column; the alternative is discovering later that the extractor learned to fill it and we have nowhere to put it |
| `is_available` | `boolean` | no | | did the capture hold the bytes |

Dropped deliberately: `position` (the block's `xpath` and `block_index` say
where it is — storing it twice invites disagreement), `size_bytes` (a property of
the file, obtainable by `stat`, and it changes nothing about the article).

### B.5 `corpus.article_video`

| Column | Type | Null | Key | Reason |
| --- | --- | --- | --- | --- |
| `id` | `bigint` | no | **PK** | |
| `article_id` | `bigint` | no | FK CASCADE, index | `get_article_videos` |
| `extraction_id` | `bigint` | no | FK CASCADE | |
| `local_ref` | `text` | no | UNIQUE `(extraction_id, local_ref)` | `video_001` |
| `platform` | `text` | yes | index (composite) | `youtube`, `facebook`, `videa`, `html5`…; 8% null (a generic iframe) |
| `external_id` | `text` | yes | index (composite) | the platform's own id. `(platform, external_id)` answers **"which articles embed this video?"** — a real journalist question, and the reason this pair is indexed |
| `source_type` | `text` | no | | the extractor's `type`; usually equals platform, differs for `html5`/`iframe` |
| `canonical_url` | `text` | yes | | the watch URL where derivable; 50% null |
| `embed_url` | `text` | no | | what the page actually embedded |
| `thumbnail_url` | `text` | yes | | recorded, never fetched |
| `title`, `caption` | `text` | yes | | 100% null today; same argument as `credit` |
| `file_path` | `text` | yes | | relative path; null when nothing playable was written |
| `is_archived` | `boolean` | no | | are the bytes in the capture. **`is_archived=true` with `file_path=null` is a valid, meaningful state**: an adaptive-stream ladder is held but not reassemblable |

**No FK from video to content block, and none the other way that is required.**
A video record can exist with no block — a player found on the page outside the
article body. The direction is `content_block.video_id → article_video`,
nullable, so a video without a block is simply a video nothing points at. Nine
of twelve videos in the sample have blocks; three do not.

Deliberately not a separate `video` dimension table keyed `(platform,
external_id)`. It would normalize a genuine shared entity, but the only query it
enables — "articles embedding this video" — is already a plain index scan on the
pair, and a dimension table would add a join to every read and an upsert to every
write. Revisit if per-video attributes (duration, uploader) ever need one home.

### B.6 `corpus.article_link`

| Column | Type | Null | Key | Reason |
| --- | --- | --- | --- | --- |
| `id` | `bigint` | no | **PK** | |
| `article_id` | `bigint` | no | FK CASCADE, index | `get_article_links` |
| `extraction_id` | `bigint` | no | FK CASCADE | |
| `content_block_id` | `bigint` | yes | FK SET NULL | the block the anchor sits in |
| `target_url` | `text` | no | | as written in the href |
| `target_url_hash` | `text` | yes | index | **canonicalized with the same function as `article.url_hash`.** This is what turns links into a research capability: "which archived articles link to this one" becomes one indexed join, and it is the natural feed for the graph layer |
| `anchor_text` | `text` | no | | |
| `context` | `text` | yes | | the owning block's text. Null 35% of the time in the pre-fix sample, but that was §H.2's synthetic anchors; it is now null only when a block genuinely carries no text |
| `is_internal` | `boolean` | no | | same-outlet vs outbound |
| selector columns | | yes | | five columns, §E |

**No unique constraint on `(article_id, target_url)`.** The same URL legitimately
appears more than once in an article with different anchor text — the extractor
dedupes on `(href, text)`, not on href. Two links to the same target from
different sentences are two citable facts.

### B.7 `corpus.article_artifact`

Document-level files. One row per file, path only.

| Column | Type | Null | Key | Reason |
| --- | --- | --- | --- | --- |
| `id` | `bigint` | no | **PK** | |
| `article_id` | `bigint` | no | FK CASCADE | |
| `extraction_id` | `bigint` | yes | FK SET NULL | null for the `.wacz`, which the crawler owns, not any extraction |
| `kind` | `text` | no | CHECK in (`readability_html`,`original_html`,`screenshot`,`wacz`), UNIQUE `(article_id, kind)` | the extractor writes exactly one of each; the unique constraint makes that an enforced fact |
| `file_path` | `text` | no | | **relative** to a configured storage root |
| `media_type` | `text` | no | | `text/html`, `image/png`, `image/webp`, `application/wacz` — a viewer needs it, and it is how "prefer a raster screenshot" is expressed in data |
| `byte_size` | `bigint` | yes | | storage accounting without stat-ing 4M files |
| `sha256` | `text` | yes | | only meaningful for the `.wacz`, where `archives.wacz_sha256` already holds it; nullable for the rest |

Image and video *files* are **not** artifact rows. They already live in tables
with the domain metadata they need (`width`, `platform`), and a generic artifact
row would add a second place where a path can disagree with itself. Artifacts are
for the document-level singletons.

`kind='screenshot'` with `media_type` carries the whole "prefer PNG/JPEG over the
webp fallback" rule as queryable data. What it cannot yet record is *which*
Browsertrix variant won (`fullPage` vs the backfill sidecar) — the extractor logs
that and does not emit it. See §Open.

### B.8 Arrays vs join tables: authors, tags, outlet

**`tags text[]` + GIN, not a join table.** The requirements are "search tags" and
"find articles by tag". `tags @> ARRAY['Tapolca']` on a GIN index answers both,
facets come from `unnest`, and ingestion stays one row per article. A `tag` +
`article_tag` pair would add two tables and a two-hop join to enable tag aliasing
and global counts — neither of which is a stated requirement. Promote it when
canonical tag identity is actually needed; the array is a superset of the data so
the migration is mechanical.

**`authors text[]` + GIN**, same reasoning, plus one specific to this system:
canonical *person* identity is Neo4j's job. Modelling an `author` dimension in
Postgres would create a second, competing notion of who a person is. Postgres
stores the byline as the page printed it; the graph decides that two bylines are
one actor.

**`outlet text`, not a table.** `urls` and `archives` already carry outlet as a
text column; a third representation would be the one that drifts. Twelve values,
indexed as part of `(outlet, published_at DESC)`.

### B.9 `corpus.passage_reference`

A citation to an exact passage. **This is the table Neo4j points at.**

| Column | Type | Null | Default | Key | Reason |
| --- | --- | --- | --- | --- | --- |
| `id` | `uuid` | no | `gen_random_uuid()` | **PK** | **uuid, not bigint**, because the claims layer must be able to mint a reference id *before* insert so a Neo4j Evidence node can be created without a Postgres round-trip. This is the one place that argument applies |
| `article_id` | `bigint` | no | | FK→`article` CASCADE, index | which article. Never CASCADEs from an extraction |
| `content_block_id` | `bigint` | yes | | FK→`content_block` **SET NULL** | a convenience pointer, allowed to go stale |
| selector columns | | | | | five columns, §E |
| `resolution_status` | `text` | no | `'unverified'` | CHECK in (`unverified`,`ok`,`repaired`,`quote_not_found`) | see §F.5 |
| `last_verified_at` | `timestamptz` | yes | | | |
| `verified_against_extraction_id` | `bigint` | yes | | FK SET NULL | which reading it last resolved against |
| `created_at` | `timestamptz` | no | `now()` | | |

Note what is absent: no FK to `article_extraction`. A citation is about the
article, not about a reading of it. That is the whole point.

### B.10 Not included, and why

| Considered | Verdict |
| --- | --- |
| `outlet`, `tag`, `author`, `video` dimension tables | no stated query needs them; §B.8, §B.5 |
| `content_block_item` for list items | 2 of 179 blocks; block-level text already searchable; §B.3 |
| `article_metadata_history` | no query needs "the title as of last month". A trigger-based history table is a later, additive change |
| block-level `checksum` / stable content key | that is `block_id` under another name, and it was removed deliberately. The selector's quote is the durable anchor |
| `article.body_text` denormalized | duplicates 11.7 GB to serve a query the block index already serves; §D |
| any binary column (`bytea`) | §G |
| pgvector / embeddings | out of scope, and the extension is not even available |

---

## C. Relationships

```
public.urls ──┐
              │ url_hash
              ▼
        corpus.article ─────────────────────────────► corpus.passage_reference
              │  ▲                                          │  (uuid PK, what
              │  │ current_extraction_id                    │   Neo4j references)
              │  │                                          │
              │  └──────────────┐                           ▼ (nullable, SET NULL)
              │                 │                     corpus.content_block
              ├── corpus.article_extraction ──┐             ▲
              │        ▲ (wacz_sha256)        │             │
              │        └── public.archives    │             │
              │                               ▼             │
              ├── corpus.content_block ───────┴─────────────┘
              │        │  image_id (nullable) ──► corpus.article_image
              │        └─ video_id (nullable) ──► corpus.article_video
              │
              ├── corpus.article_image
              ├── corpus.article_video      (may have NO block)
              ├── corpus.article_link ──► content_block (nullable)
              │        └─ target_url_hash ──► urls / article  (soft join)
              └── corpus.article_artifact
```

Ownership in one sentence each:

* `article` owns everything and is never deleted by extraction.
* `article_extraction` owns the content: blocks, images, videos, links.
  Deleting an extraction deletes its content, by design.
* `passage_reference` hangs off `article`, not off any extraction, so
  re-extraction cannot cascade a citation away.

---

## D. Search strategy

### D.1 Language configuration — measured, not assumed

`hungarian` exists and stems real Hungarian agglutination correctly:

```
to_tsvector('hungarian',
  'Zrínyire a japánok is felnéznek, az 1566-os csatában tanúsított hősiessége')
→ '1566':7 'csat':9 'felnéz':5 'hősiesség':11 'is':4 'japán':3 'os':8
  'tanúsítot':10 'zríny':1

to_tsvector('simple', 'Zrínyire a japánok is felnéznek')
→ 'a':2 'felnéznek':5 'is':4 'japánok':3 'zrínyire':1      -- no stemming
```

`Zrínyire → zríny`, `csatában → csat`, `japánok → japán` — that is exactly what a
search for "Zrínyi" needs, and `simple` cannot do it. Confirmed:
`to_tsvector('hungarian','Szigetvár kazamatái') @@ to_tsquery('hungarian','Szigetvár')`
is true.

**But `hungarian` preserves accents** (`zríny`, not `zriny`). A journalist on a
non-Hungarian keyboard typing `Zrinyi` gets nothing. So:

```sql
CREATE EXTENSION unaccent;                       -- available, not yet installed
CREATE TEXT SEARCH CONFIGURATION hungarian_ci ( COPY = hungarian );
ALTER  TEXT SEARCH CONFIGURATION hungarian_ci
  ALTER MAPPING FOR hword, hword_part, word WITH unaccent, hungarian_stem;
```

> **Superseded by migration 007.** The reasoning above is sound about the
> problem and wrong about the fix — see D.1.2. `hungarian_ci` still exists but
> no vector uses it. The rest of this section (immutability, the config being
> part of the schema contract, a change being a migration rather than a tweak)
> applies unchanged to what replaced it.

`hungarian_ci` was the config every vector used. It must be created **before**
any generated column references it, and it must never be altered afterwards — a
stored `tsvector` is not recomputed by an `ALTER CONFIGURATION`, so changing it
silently desynchronizes the index from the data. Changing it later means a
rewrite of both vectors, which is a migration, not a tweak.

One caveat to state plainly: a stored generated column requires the config to be
`IMMUTABLE`-referenced by name, which Postgres permits, but it also means the
config becomes part of the schema's contract. That is the right trade for
correct Hungarian search.

### D.1.2 Why `unaccent → hungarian_stem` was the wrong order

Putting `unaccent` in front of the stemmer hands the stemmer text that is **no
longer Hungarian**. Snowball then strips whatever the accent-free spelling makes
look like a suffix:

| word | `hungarian_ci` lexeme | |
| --- | --- | --- |
| `Orbán` | `or` | `-ban` read as the inessive case |
| `orra` ("nose") | `or` | …so the two collide |
| `Orbánnak` | `orban` | …and matches neither |
| `Magyarország` | `magyarorszag` | |
| `Magyarországról` | `magyarorszagrol` | the pair never unifies |

The damage is not uniform — `kormány`/`kormányban` unify correctly while
`Orbán`/`Orbánnak` do not — so the behaviour cannot be predicted from the query,
which is the worst property a search system can have. On the 36-article dev
corpus `Magyarországról` returned 2 articles of 20 and `Orbánnak` returned none.

Accent-insensitivity and stemming are two jobs, and one lexeme per word cannot
do both. **007 indexes two**, unioned into the same vector:

| | built from | `kormányban` → |
| --- | --- | --- |
| lemma | stem the accented text, *then* fold accents off the lemma | `kormany` |
| surface | fold accents off the word, no stemming | `kormanyban` |

A query is expanded the same way and alternated per term — `kormányban` becomes
`('kormany' | 'kormanyban')` — so the lemma side carries inflection, the surface
side carries accent-free spelling, and neither can be broken by the other's
failure mode. `corpus.search_vector()` builds the vector, `corpus.search_query()`
builds the query, and the two must always change together.

Measured over 19 probe queries against an accent-folded substring yardstick
computed in Python (`scripts/stemming_lab.py`, so no configuration can score
well by agreeing with itself): recall **70.2% → 93.1%**, precision
**93.4% → 95.2%**, both GIN indexes roughly doubling. Six other candidates were
scored, including two built on hunspell `hu_HU`; they are tabulated in the
header of `migrations/007_search_recall.sql`. The two hunspell options were
rejected for needing dictionary files in `$SHAREDIR/tsearch_data` inside the db
container on milab2, surviving every rebuild, to buy 1.2 points of recall and
lose 4.2 of precision.

The cost is real: two lexemes per word, and rebuilding the generated columns
rewrites every row of both tables — ~58M blocks and ~11.7 GB of text in
production. This is a maintenance-window migration, not an online one.

### D.1.1 A generated column needs strict immutability

`array_to_string` is declared **STABLE, not IMMUTABLE** — in general it depends
on the element type's output function. A `STORED` generated column requires a
strictly immutable expression, so the obvious spelling fails outright:

```
ERROR:  generation expression is not immutable
```

Found by applying the migration to a throwaway database, not by reading the
docs. Narrowed to `text[]` with a constant separator the operation genuinely is
deterministic, so migration 001 declares a wrapper:

```sql
CREATE FUNCTION corpus.text_array_to_string(arr text[]) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $$ SELECT array_to_string(arr, ' ') $$;
```

That is a correct declaration for this concrete type rather than a lie to get
past the planner, and it must not be widened to `anyarray`. The two-argument
`to_tsvector(regconfig, text)` is already immutable; the one-argument form is
only STABLE and cannot be used in a generated column at all, which is why the
config is spelled out everywhere.

### D.2 Three vectors, and why not four

**`article.search_tsv`** — for *finding articles*:

```sql
setweight(corpus.search_vector(coalesce(title,'')),                          'A') ||
setweight(corpus.search_vector(coalesce(subtitle,'')),                       'B') ||
setweight(corpus.search_vector(coalesce(description,'')),                    'B') ||
setweight(corpus.search_vector(corpus.text_array_to_string(authors)),        'C') ||
setweight(corpus.search_vector(corpus.text_array_to_string(tags)),           'C')
```

Weights are the point: a title hit must outrank a tag hit. This also gives fuzzy
author and tag search for free, which is why there is no separate author index.

**`content_block.text_tsv`** — for *finding and citing passages*:

```sql
corpus.search_vector(coalesce(text,''))
```

**`article_image.caption_tsv`** — for *finding pictures by what they show*
(migration 008):

```sql
corpus.search_vector(coalesce(caption,'') || ' ' || coalesce(alt,'')
                                          || ' ' || coalesce(credit,''))
```

`block_text` is NULL for image and video blocks by design — an image block
points at a row, it does not carry prose — so before 008 those blocks held an
empty vector and 15% of all blocks could never match anything, with every image
caption invisible to search. Caption text could not join `article.search_tsv`
because a `STORED` generated column may only reference its own row, and it must
not join `block_text` because the citation chain rests on
`block_text = normalize_text(element)` and a caption lives in a `<figcaption>`
that is not the block's element. So it gets its own vector, reached by the same
semi-join as the body.

It is **discoverable but not citable**: there is no selector for a caption, so a
caption match tells you which article and which image and stops there. Measured
over 277 query variants, that is 80 hits that now come back and cannot be
pointed at — a deliberate trade, and the reason `search_articles` reports
`match_reason = 'caption'` so a caller can tell the two apart.

**There is deliberately no fourth vector over the whole article body.**
`search_articles(query)` matching body text is served by a semi-join:

```sql
SELECT a.* FROM corpus.article a
WHERE a.search_tsv @@ q
   OR EXISTS (SELECT 1 FROM corpus.content_block b
              WHERE b.article_id = a.id AND b.text_tsv @@ q);
```

An article-level body vector would duplicate 11.7 GB of text into a second GIN
index to serve a query this already answers. The trade-off worth naming: whole-
document ranking (`ts_rank` over the full body) is not available, and ranking
instead aggregates the best block scores. For a citation-oriented tool that is
arguably the better ranking anyway — the best passage is what you want to show.
If whole-document ranking turns out to matter, add it then, with a measurement.

### D.3 Indexes, one query each

| Index | Query it serves | Needed now? |
| --- | --- | --- |
| `article(url_hash)` UNIQUE | find an article by URL — the single most common lookup, and the ingestion upsert target | yes |
| `article(canonical_url)` btree | resolve a pasted URL that is the canonical rather than the crawled one | yes |
| `article(outlet, published_at DESC NULLS LAST)` | "latest from origo.hu" — the default browse, and per-outlet corpus stats | yes |
| `article USING gin(search_tsv)` | `search_articles(query)` | yes |
| `article USING gin(tags)` | exact tag filter, `tags @> ARRAY['Tapolca']`. **Not** redundant with `search_tsv`: a tsv match on 'Tapolca' also matches body-adjacent text, which is wrong for a filter | yes |
| `article USING gin(authors)` | exact byline filter | **defer** — fuzzy author search is already in `search_tsv`; add when an exact filter is actually requested |
| `content_block(article_id, block_index)` | `get_article_content` in document order; the hot path | yes |
| `content_block USING gin(text_tsv)` | `search_article_content(query)`, and article body search via the semi-join | yes |
| `content_block(image_id)`, `(video_id)` partial | **FK maintenance**, not the reverse lookup — see §D.4 | yes (**corrected**: was deferred, wrongly) |
| `article_image(article_id)` | `get_article_images` | yes |
| `article_video(article_id)` | `get_article_videos` | yes |
| `article_video(platform, external_id)` | "which articles embed this YouTube video" | yes |
| `article_link(article_id)` | `get_article_links` | yes |
| `article_link(target_url_hash)` | inbound links; "what links to this article"; the feed for graph edges | yes |
| `article_extraction(article_id)` | extraction history for an article | yes |
| `article_extraction(article_id) WHERE is_current` UNIQUE | enforces one live reading; also the lookup | yes |
| `article_extraction(wacz_sha256)` | "re-extract everything read from this capture" | yes |
| `passage_reference(article_id)` | citations on an article | yes |
| `passage_reference(resolution_status)` WHERE status <> 'ok' | the repair queue after a re-extraction | yes |
| `article_artifact(article_id, kind)` UNIQUE | fetch the `readability.html` path for the viewer | yes |
| `pg_trgm` on title | substring/typo title search | **defer** — extension not installed; `search_tsv` covers the stated need |

### D.4 The index class I initially got wrong

Writing the migration surfaced an error in the table above. I had deferred
`content_block(image_id)` and `(video_id)` on the grounds that the reverse
lookup ("which block shows this image") is cheap when scoped to an article. That
reasoning was about the *read*, and it missed the *write*.

**PostgreSQL does not index the referencing side of a foreign key.** It indexes
the referenced side — the parent key — automatically, and never the child. So
every `ON DELETE CASCADE` and `ON DELETE SET NULL` performs a lookup on an
unindexed column unless one is created by hand.

That matters far more here than a reverse lookup ever could, because
re-extraction deletes a superseded extraction on every re-run — up to 4.2M times.
That single delete fires:

* `CASCADE` into `content_block`, `article_image`, `article_video`, `article_link`
* `SET NULL` on `article.current_extraction_id`,
  `content_block.image_id` / `video_id`, `article_link.content_block_id`,
  `passage_reference.content_block_id`, `article_artifact.extraction_id`,
  `passage_reference.verified_against_extraction_id`

Each unindexed one is a sequential scan of its table, and `content_block` is
58M rows. So migration 006 creates a **second class of index**, justified by
referential action rather than by a query:

| Index | Referential action it serves |
| --- | --- |
| `article(current_extraction_id)` partial | SET NULL on every supersede |
| `content_block(image_id)`, `(video_id)` partial | SET NULL when media rows are deleted |
| `article_link(extraction_id)` | CASCADE — this table has no UNIQUE to lean on |
| `article_link(content_block_id)` partial | SET NULL when blocks are replaced |
| `article_artifact(extraction_id)` partial | SET NULL on supersede |
| `passage_reference(content_block_id)` partial | SET NULL when blocks are replaced |
| `passage_reference(verified_against_extraction_id)` partial | SET NULL on supersede |
| `article_extraction(archive_row_id)` partial | SET NULL if `archives` is ever pruned |

Where a `UNIQUE` constraint already leads with the referencing column, no extra
index is created: `content_block(extraction_id, block_index)`,
`article_image(extraction_id, local_ref)` and
`article_video(extraction_id, local_ref)` each serve their own cascade.

All partial (`WHERE col IS NOT NULL`), because the columns are mostly null — the
media indexes cover ~7.6M of ~58M blocks.

Final count: **24 indexes**, of which 15 serve a named read and 9 serve a
referential action. Both kinds are annotated in `migrations/006_indexes.sql`.

### D.5 Operational notes

`CONCURRENTLY` is the right advice for *adding* an index to a populated table,
and the wrong advice for migration 006, where the tables are empty — it would
only add overhead and forbid the surrounding transaction. Migration 006
therefore uses plain `CREATE INDEX`, and its header says to skip the file during
setup and apply it with `CONCURRENTLY` afterwards if you intend a bulk backfill
first, which is the right order for a multi-day initial ingestion.

Consider `fastupdate=off` on the block GIN if ingestion makes search latency
spiky. Not set by default: it is a tuning decision that wants a measurement.

---

## E. Selector strategy

### E.1 Representation: normalized columns, JSON at the edge

Five columns, used identically on `article_link` and `passage_reference`:

| Column | Type | Null | Reason |
| --- | --- | --- | --- |
| `selector_xpath` | `text` | yes | `/html/body/article/div/p[3]` |
| `quote_start` | `int` | yes | inclusive, over normalized text |
| `quote_end` | `int` | yes | exclusive |
| `quote_exact` | `text` | yes | the passage — **the authority** |
| `quote_prefix` | `text` | yes | ≤32 chars before |
| `quote_suffix` | `text` | yes | ≤32 chars after |

All nullable together (`links.selector` can be null when the anchor text is not
locatable), with a table CHECK that they are all-present or all-absent:

```sql
CHECK ( (selector_xpath IS NULL AND quote_start IS NULL AND quote_exact IS NULL)
     OR (selector_xpath IS NOT NULL AND quote_start IS NOT NULL
         AND quote_end > quote_start AND quote_exact IS NOT NULL) )
```

Not stored as `type`/`refinedBy.type` columns: both are constants
(`XPathSelector`, `TextPositionSelector`) in every record the extractor emits.
Storing a constant 4M times to reproduce a wire format is not data modelling.
They are re-added by the API layer.

**Why columns and not JSONB.** The selector is a fixed-arity tuple of six
scalars. Columns give it a CHECK constraint, a plain `WHERE quote_exact = $1`
lookup, correct statistics for the planner, and no risk of a malformed selector
entering the table. JSONB would need an expression index for the same lookup and
would accept `{"nonsense": true}` silently. The hybrid is at the *edge*: a view
assembles the canonical shape so an MCP tool returns it without the application
re-deriving it.

```sql
CREATE VIEW corpus.passage_selector AS
SELECT id, article_id, content_block_id, resolution_status,
       jsonb_build_object(
         'type', 'XPathSelector',
         'value', selector_xpath,
         'refinedBy', jsonb_build_object('type','TextPositionSelector',
                                         'start', quote_start, 'end', quote_end),
         'quote', jsonb_strip_nulls(jsonb_build_object(
                    'exact', quote_exact, 'prefix', quote_prefix,
                    'suffix', quote_suffix))
       ) AS selector
FROM corpus.passage_reference;
```

A view costs nothing to store and cannot drift from the columns. A
`GENERATED ... STORED` jsonb column would duplicate the bytes for the same result.

### E.2 Retrieving and validating a passage

`get_passage(reference_id)` returns, in one query: the reference, its selector,
the article's `url_hash`, the `readability.html` artifact path, and the current
text of the block it points at. The frontend then does exactly what the
extractor's own verifier does:

```
readability.html  →  document.evaluate(selector_xpath)
                  →  normalize(element.textContent)      -- the documented function
                  →  slice(quote_start, quote_end)
                  →  compare with quote_exact
     equal        →  scroll into view, highlight
     not equal    →  DO NOT highlight; report a mismatch, and optionally
                     re-find by quote_prefix + quote_exact + quote_suffix
```

The database never resolves XPath itself — it has no DOM. It stores the evidence
and the artifact path; resolution happens where the document is. What the
database *can* do cheaply is the text half: given a block's `text`, verify that
`substring(text from quote_start+1 for quote_end-quote_start) = quote_exact`.
That is the check the verification job in §F.5 runs, in SQL, with no browser.

---

## F. Re-extraction strategy

Ingestion of one article is **one transaction**, so a reader never sees a
half-replaced article.

### F.1 The same `.wacz`, the same extractor version

Output is byte-identical — verified twice on the full set, `diff -r` clean apart
from `extracted_at`. Ingestion is therefore idempotent by construction:

```
UPSERT corpus.article       ON CONFLICT (url_hash) DO UPDATE   -- metadata refresh
INSERT corpus.article_extraction (is_current = false)
INSERT content_block / article_image / article_video / article_link
UPDATE the new extraction SET is_current = true
UPDATE the previous extraction SET is_current = false
DELETE the previous extraction's content (see F.2)
UPDATE corpus.article SET current_extraction_id = <new>
```

Insert-then-flip, not delete-then-insert: the old content stays readable until
the new content is complete and committed. The partial unique index on
`is_current` makes the flip atomic and makes two concurrent ingestions of the
same article impossible to both win.

### F.2 The extractor version changes

Identical flow. The only question is retention of superseded content.

**Recommendation: keep the superseded `article_extraction` row, delete its
content.** The row is ~100 bytes and preserves the audit trail ("this article was
read by v2.0.0 on 2026-08-31, status partial"). Its 14 blocks are ~4 KB, and at
4.2M articles a retained generation is ~12 GB of text plus its share of the GIN
index — paid again per version. Nothing queries old blocks: citations do not
reference them (§A.2), and the artifacts they described have been overwritten.

Keep-N-generations is a config knob, not a schema change, so this is reversible.

### F.3 The same article captured again

The corpus writes one `page.wacz` per URL, so a re-archive overwrites in place
and the article row is unchanged — one `url_hash`, one `article`. The new reading
records the new `wacz_sha256`, which makes the change visible and queryable:
"every extraction whose `wacz_sha256` no longer matches `archives`" is the
re-extraction backlog.

If the corpus ever keeps multiple captures per URL, nothing breaks:
`article_extraction` already carries capture identity, so N captures × M versions
is N×M extraction rows under one article.

### F.4 Article metadata changes

UPSERT in place. `row_updated_at` moves; `first_ingested_at` does not. Title and
tags are not citation targets, so correcting them is safe — that is precisely why
they live on `article` and not on the extraction. No history table for now (§B.10).

### F.5 A selector becomes invalid

The case the whole design exists for. After an extraction flips to current, a
verification pass walks that article's `passage_reference` rows:

| Outcome | Meaning | Action |
| --- | --- | --- |
| `ok` | xpath resolves and `quote_exact` matches | stamp `last_verified_at` |
| `repaired` | xpath drifted, but the quote was re-found by prefix+exact+suffix | write the corrected `selector_xpath` and offsets; keep the quote untouched |
| `quote_not_found` | the passage is not in the new document | leave the reference intact, flag it, surface it to a human. **Never repoint it** |

`resolution_status` starts at `unverified` and is never silently assumed. The
partial index on `resolution_status <> 'ok'` is the repair queue.

Two properties this gives, both grounded in the 43%/99% measurement: a drifted
citation is *detected* rather than silently mis-highlighted, and in the large
majority of cases it is *repaired automatically* — because the quote, not the
xpath, is what the database stores as the anchor.

The pure-text half of this check is SQL, no DOM needed:

```sql
SELECT p.id,
       substring(b.text from p.quote_start + 1
                 for p.quote_end - p.quote_start) = p.quote_exact AS still_exact
FROM corpus.passage_reference p
JOIN corpus.content_block b ON b.id = p.content_block_id
WHERE p.article_id = $1;
```

---

## G. Artifact strategy

**No binaries in Postgres.** The corpus is ~36 TB on `/mnt/hdd`; the database
lives on the same disk and shares it with the WAL. Moving 11.7 GB of HTML and
terabytes of media into rows would multiply WAL volume, break `pg_dump`, and — as
already learned the hard way on this project — a full disk is a *correctness*
risk here, not merely an availability one. `bytea`/large objects are out.

What Postgres stores is a **relative path plus enough metadata to serve the file
without opening it**:

| Artifact | Where | How referenced |
| --- | --- | --- |
| `readability.html` | filesystem | `article_artifact` kind=`readability_html`, `media_type='text/html'` |
| `original.html` | filesystem | `article_artifact` kind=`original_html` |
| `screenshot.png` / `.webp` | filesystem | `article_artifact` kind=`screenshot`, `media_type` distinguishes raster from the webp fallback |
| `images/image_001.jpg` | filesystem | `article_image.file_path` |
| `videos/video_001.mp4` | filesystem | `article_video.file_path` |
| `page.wacz` | filesystem | `article_artifact` kind=`wacz`, `sha256` mirrors `archives.wacz_sha256` |

**Paths are relative, never absolute.** The same corpus is mounted at different
paths on milab2 and on milab4 (sshfs), and was read in this session over a third
mount point. An absolute path would encode one machine's view and break on the
others. The storage root is configuration; the database stores
`<outlet>/<h2>/<url_hash>/readability.html`.

`byte_size` is stored so storage accounting and "is this artifact unexpectedly
tiny" checks do not require stat-ing 4M files. `sha256` is stored only where it is
the evidence anchor — the `.wacz` — and is nullable elsewhere rather than
computing a hash of every HTML file for no consumer.

Consequence worth stating: because the paths are relative and the `.wacz` is a
row like any other, **deleting the `.wacz` layer later is a data-retention
decision, not a schema change.** Drop the `kind='wacz'` rows, keep everything
else. That is the property §11 asks for.

---

## H. Migration strategy

The crawler must not be touched. Four properties make that true by construction:

1. **A new schema, `corpus`.** Zero name collisions with `public` (checked:
   `videos` is already taken there, which is exactly the kind of collision a
   separate schema prevents). It can be granted, dumped and restored
   independently, and `DROP SCHEMA corpus CASCADE` is a complete, safe rollback
   during development.
6. **New migration files only** — `009_corpus_schema.sql` onwards, applied by the
   existing `scripts/migrate.py`, which already tracks applied versions in
   `schema_migrations`. No existing migration is edited.
3. **No `ALTER` on `urls`, `archives` or `videos`.** The only touch point is the
   outbound FK from `corpus.article.url_hash` to `urls(url_hash)` (§B.1.1). It
   adds no column, no index and no trigger to `urls`, and needs no validation
   scan because the referencing table is empty when it is created.

   One honest caveat: creating a table that references `urls` takes a
   `SHARE ROW EXCLUSIVE` lock on `urls` for the duration of the statement, and
   that lock conflicts with the `ROW EXCLUSIVE` that `INSERT`/`UPDATE` take. So
   crawler writes to `urls` block for as long as the DDL runs — sub-second here,
   since there is nothing to validate. Run migration 010 with a short
   `lock_timeout` (say `SET lock_timeout = '5s'`) so it fails fast and retries
   rather than queueing behind a long-running transaction and holding the
   crawler off. Archiving is complete and the fleet is idle, which makes this
   moot today; the note is for the next time it is not.
4. **No worker rebuild.** The crawler image copies `causalia/`, `scripts/`,
   `migrations/` and `run.py`. Adding a migration file changes the image layer,
   so apply it with the existing `migrate.py` **from a host checkout**, not by
   rebuilding and recreating the 72-worker fleet. Archiving is complete and the
   fleet is idle, but the rule stands.

**Written and validated.** `migrations/001`–`006`, applied by
`scripts/migrate.sh` or plain `psql -f`:

```
007_search_recall.sql       hungarian_lemma + hungarian_surface configs,
                            search_vector, search_query; rebuilds both vectors
001_corpus_schema.sql       schema, ledger, unaccent, corpus.hungarian_ci,
                            the immutable array helper (§D.1.1)
002_article.sql             article + article_extraction (the spine)
003_content.sql             content_block, article_image, article_video,
                            article_link
004_artifacts.sql           article_artifact
005_passage_reference.sql   passage_reference + corpus.passage_selector view
006_indexes.sql             24 indexes: 15 query, 9 FK-maintenance (§D.4)
```

Two deviations from the sketch above, both deliberate:

* **Numbered from 001 with its own ledger, `corpus.schema_migrations`** — not
  009 onwards in the crawler's `public.schema_migrations`. Two independent
  sequences cannot collide, applying these files never writes to a table the
  crawler reads, and the migrations live in this repository rather than in
  `causalia-final`, whose `migrations/` directory is `COPY`'d into the worker
  image and whose working tree currently holds undeployed changes.
* **Plain `CREATE INDEX`, not `CONCURRENTLY`** — see §D.5. Splitting indexes into
  their own file still matters for the reason given: skip 006 during setup, bulk
  backfill, then apply it.

Every file is idempotent — `IF NOT EXISTS` throughout, guards where PostgreSQL
offers no such clause, and a ledger insert with `ON CONFLICT DO NOTHING` — so
`psql -f` twice is a no-op.

### H.1 Validation — done, not planned

Two harnesses, both against a throwaway `postgres:16` container. **Neither
touches milab2 or any real database.**

`scripts/validate_migrations.sh` — applies all six from scratch, then **applies
them a second time** to prove idempotency, then `DROP SCHEMA corpus CASCADE` to
prove the rollback is complete:

```
001…006  OK   (twice)
tables: 9   views: 1   indexes: 39   ts_config: 1
DROP SCHEMA corpus CASCADE: OK
public tables surviving: 4 (expected 4)
```

(39 `pg_indexes` rows = the 24 created explicitly plus the implicit ones behind
the primary keys and `UNIQUE` constraints.)

`scripts/validate_ingestion.sh` — ingests the real 13-article sample and asserts
the extractor's guarantees still hold *through* the database, then ingests
everything a second time to exercise re-extraction:

```
907 checks run
ALL CHECKS PASSED

article 13   article_extraction 26   content_block 179
article_image 23   article_video 12   article_link 13
article_artifact 39   passage_reference 1
```

What those checks cover, beyond row counts:

* **Invariant A through the database** — every `block_text` read back out of
  Postgres equals `normalize_text()` of the element its stored `xpath` selects
  in `readability.html`. If ingestion mangled a Hungarian character or an
  offset, this is where it would show.
* **Invariant B twice** — once in pure SQL
  (`substring(block_text from start+1 for end-start) = quote_exact`, which is
  the check the repair job runs at scale) and once against the real document the
  way a frontend would.
* **Invariants C and D** — no media block resolves to a missing record.
* **`block_index` contiguity** — 1..n with no gaps, which is what makes it
  usable as reading order and what would catch a truncated ingestion.
* **Exactly one current extraction** per article, and
  `article.current_extraction_id` never pointing at a non-current row.
* **Hungarian search really works** — `Zrínyi` finds it (stemming), `zrinyi`
  finds it (unaccent), `Szigetvár` finds it via a *tag* rather than the title,
  `kazamata` finds it via a *paragraph* through the block index, and the exact
  tag filter `tags @> ARRAY['Szigetvár']` is not satisfied by a body mention.
* **The CHECK constraints bite** — a `passage_reference` whose `quote_exact`
  length disagrees with its offsets is *rejected*, not stored.
* **Re-extraction** — 13 articles ingested twice produce 13 article rows and 26
  extraction rows; superseded content is gone; no duplicate articles.
* **A citation survives re-extraction** — the `passage_reference` is still
  there, its `content_block_id` was correctly `SET NULL` when the block it
  pointed at was replaced, and its `quote_exact` still validates in SQL against
  the *new* extraction's block at the same xpath. That is the design's central
  claim, demonstrated rather than asserted.

### H.2 A finding the harness produced

**35% of `links.json` is the extractor citing itself.** Seven of the twenty links
in the sample have an anchor text like
`youtube: https://www.youtube.com/embed/84vDSPsif5Y` and a selector pointing at
a *video* block. Those are not links a journalist wrote — they are the reader
view's own fallback anchors, which `dom.py` composes as
`"%s: %s" % (platform, url)` inside `<div class="embed">` so an offline page can
reach a player it must never auto-load. `links.py` then reads them back as
article links. It is exactly the set of rows whose `context` is null, which is
why that rate was 35%.

This surfaced as a failing assertion rather than as a plausible-looking row: the
selector targets a block whose `block_text` is null by design, so the SQL quote
check had nothing to compare.

**Fixed at the source, 2026-09-01.** `links.extract_links` now skips an anchor
whose owning block is an image or video block, so the extractor no longer
records an element its own renderer created. Two tests in
`tests/test_links.py::TestMediaBlocks` pin it: the embed anchor is absent, and a
prose link in the same article is still recorded.

The ingestion filter stays, as a guard rather than a fix — output extracted
before 2026-09-01 still carries these rows, and the ingestion layer must not
import them. On post-fix output its counter reads zero, which is the check that
the two layers agree. Nothing is lost either way: the URL is already on the
video row as `embed_url`, and the canonical watch URL is on `url` — the anchor
carried only the *embed* form.

---

## Decisions taken

| Decision | Date | Where |
| --- | --- | --- |
| `corpus.article.url_hash` gets a real FK to `urls(url_hash)`, `ON DELETE RESTRICT` | 2026-08-31 | §B.1.1, §H.3 |
| The synthetic embed links are fixed in `links.py`, not just filtered at ingestion; the ingestion filter is kept as a guard for pre-fix output | 2026-09-01 | §H.2 |

## Open questions

Things I would rather decide with you than assume.

1. **`wacz_sha256` on the extraction row.** It is the cleanest tie between a
   reading and the capture it read, and it makes the re-extraction backlog a
   query — but the extractor does not emit it, deliberately (computing it means a
   second full read of ~36 TB). Options: read it from `archives.wacz_sha256` at
   ingestion (free, and that column is already populated), or leave the column
   null. I recommend the former.
2. **Screenshot provenance.** `article_artifact` can record the file and its
   media type but not *which* Browsertrix variant won (`fullPageFinal` /
   `fullPage` / `view` / `thumbnail` / backfill sidecar). The extractor knows and
   logs it. Adding one field to `extraction.json` would make it storable — a
   small extractor change, for after this design is approved.
3. **The `archive_id` name.** It is `urls.url_hash`. Renaming it in the JSON
   would make the contract self-describing, at the cost of a breaking change to
   an output format we just stabilized.
4. **List items.** Skipping `content_block_item` (§B.3) means a citation can
   address a list but not a single `<li>`. Two of 179 blocks are lists, so I
   would wait for a real need.
5. **Superseded content retention** — §F.2. I recommend delete-on-supersede with
   a keep-N knob; the alternative costs ~12 GB plus index per retained
   generation.
