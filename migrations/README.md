# Corpus migrations

Plain, idempotent SQL. Design and rationale: [../docs/postgres-schema.md](../docs/postgres-schema.md).

| File | Creates |
| --- | --- |
| `001_corpus_schema.sql` | `corpus` schema, `corpus.schema_migrations` ledger, `unaccent`, `corpus.hungarian_ci` search config, `corpus.text_array_to_string` |
| `002_article.sql` | `article`, `article_extraction` |
| `003_content.sql` | `content_block`, `article_image`, `article_video`, `article_link` |
| `004_artifacts.sql` | `article_artifact` |
| `005_passage_reference.sql` | `passage_reference`, `corpus.passage_selector` view |
| `006_indexes.sql` | 24 indexes — 15 query, 9 FK-maintenance |

## What these do and do not touch

* Everything lives in the **`corpus` schema**. `public` gains nothing.
  `public.videos` already exists, which is exactly the collision a separate
  schema prevents.
* The ledger is **`corpus.schema_migrations`**, not the crawler's
  `public.schema_migrations`. Two independent sequences cannot collide, and
  applying these files never writes to a table the crawler reads.
* The only touch point outside `corpus` is one outbound foreign key,
  `corpus.article.url_hash → public.urls(url_hash) ON DELETE RESTRICT`. It adds
  no column, index or trigger to `urls`.
* **Rollback is `DROP SCHEMA corpus CASCADE`** plus `DROP EXTENSION unaccent` if
  it is unwanted. Verified to leave the four `public` tables intact.

## Applying

Idempotent: `psql -f` twice is a no-op. Apply in filename order.

```bash
scripts/migrate.sh --status      # what is applied, what is pending
scripts/migrate.sh --dry-run
scripts/migrate.sh               # apply outstanding files
```

Or by hand — the files are ordinary SQL with no runner requirements:

```bash
for f in migrations/[0-9]*.sql; do psql -v ON_ERROR_STOP=1 -f "$f"; done
```

On milab2, psql lives inside the db container. `$MILAB2` is the archiver
host's `user@host` — it is not written down here, see the deployment notes:

```bash
ssh "$MILAB2" 'cd ~/causalia-final && \
  docker compose exec -T db psql -U causalia -d causalia -v ON_ERROR_STOP=1' \
  < migrations/001_corpus_schema.sql
```

**One caveat before applying to the live database.** Creating `corpus.article`
takes a `SHARE ROW EXCLUSIVE` lock on `public.urls`, which conflicts with the
`ROW EXCLUSIVE` that `INSERT`/`UPDATE` take — so crawler writes to `urls` block
while `002` runs. There is nothing to validate (the referencing table is empty),
so it is sub-second; `002` sets `lock_timeout = '5s'` so it fails fast rather
than queueing behind a long transaction. Archiving is complete and the fleet is
idle, which makes this moot today.

`001` needs superuser for `CREATE EXTENSION unaccent`. The `causalia` role has
it (checked: `rolsuper = t`).

## Validating

Both scripts use a throwaway `postgres:16` container and **never touch milab2**.

```bash
scripts/validate_migrations.sh          # apply twice, then prove the rollback
scripts/validate_ingestion.sh <dir>     # ingest real output, assert invariants
```

`validate_ingestion.sh` takes a directory of extractor output — the same thing
`causalia-extractor extract --output` produces. It ingests every article, checks
the extractor's guarantees still hold once the data is in Postgres, then ingests
everything again to exercise re-extraction. Last run: **907 checks, all passing.**

## If you change the search configuration

`corpus.hungarian_ci` is referenced **by name** from `STORED` generated tsvector
columns. A stored generated column is not recomputed when the configuration
changes, so `ALTER TEXT SEARCH CONFIGURATION` after data exists silently
desynchronises every vector from its own text. Changing it means: drop the
generated columns, alter the config, re-add the columns, rebuild the GIN
indexes. That is a migration, not a tweak.
