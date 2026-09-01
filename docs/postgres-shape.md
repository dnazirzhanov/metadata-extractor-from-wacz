# The PostgreSQL boundary

> **Superseded by [postgres-schema.md](postgres-schema.md)** (2026-08-31), which
> is the reviewable design: entity model, every table and column, the search and
> selector strategies, and re-extraction behaviour. This file is kept only as the
> record of what was sketched before the extractor output was stable.

**Nothing here is implemented, and no migration is included.** This records the
shape the extractor's output is designed to land in, so that the schema can be
written *against real extractor output* rather than against assumptions about
it. The right moment to write it is after a run over real captures has been
inspected.

Extraction and persistence stay separate:

```
WACZ  →  Extractor  →  normalized extraction result (files)  →  ingestion  →  PostgreSQL
```

That separation is what makes it possible to test extraction on its own, re-run
it, validate output before insertion, migrate the schema independently,
reprocess old WACZ files, and eventually delete WACZ files without changing the
logical article model.

## What already exists

The live database has `urls`, `archives` and `videos`. `urls.url_hash` is the
same value this extractor emits as `archive_id`, so articles join to what is
already there without a new identifier. **Nothing currently writes extraction
output to Postgres** — an ingestion layer would be the first in the project.

`videos.platform` and `videos.external_id` already exist with a unique index on
`(platform, external_id) WHERE status='success'`, and `videos.json` is named to
match.

## The natural table shape

| Table | From | Key |
| --- | --- | --- |
| `articles` | `article.json` + `extraction.json` | `archive_id` |
| `article_blocks` | `content.json` | `(archive_id, index)` |
| `article_images` | `images.json` | `(archive_id, id)` |
| `article_videos` | `videos.json` | `(archive_id, id)` |
| `article_links` | `links.json` | `(archive_id, n)` |
| `evidence_selectors` | selector payloads | referenced by whatever cites them |

A selector is four scalars plus context — `xpath`, `start`, `end`,
`quote_exact`, `quote_prefix`, `quote_suffix` — so it stores as columns, not as
JSON, and can be verified in SQL as easily as in Python.

## Search

The first implementation should be conventional PostgreSQL full-text search over
`article_blocks.text`, which is where the searchable prose is. Article text in
this corpus is Hungarian throughout, so `to_tsvector('hungarian', text)` is the
configuration; the English field names are our schema, not the content language.

Add indexes for query patterns that exist, not for every column. A GIN index on
the block tsvector is the one that earns its place first.

**Explicitly out of scope for now:** pgvector, embeddings, semantic search,
vector databases, LLM-based retrieval. Conventional search should be evaluated
first.

Neo4j is handled separately by another developer and is not this extractor's
concern.

## Two things to settle before writing the schema

1. **Do blocks get a stable key across re-extractions?** `index` shifts whenever
   extraction improves. Stored evidence keyed on `(archive_id, index)` would
   silently repoint. The selector's `quote.exact` makes such drift *detectable*;
   whether the schema should also carry something re-findable is a decision to
   make with real data in front of us.
2. **Videos without a block.** Some video records legitimately have no content
   block (a player outside the article body). The `article_videos` row must not
   assume a block exists.
