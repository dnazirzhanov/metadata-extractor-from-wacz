"""Production ingestion: the work ledger, the extraction worker, the loader.

WHY THIS IS NOT INSIDE causalia_extractor
-----------------------------------------
The extractor package opens no socket and no database connection, and
tests/test_pipeline.py asserts it. Everything here is a database client, so it
lives outside the package for the same reason scripts/ and service/ do. The
dependency direction is one-way and must stay that way:

    ingest/  ->  causalia_extractor       (extraction is a pure function)
    ingest/  ->  scripts/cx_ingest.py     (one copy of the INSERTs)

Nothing in causalia_extractor imports anything from here.

WHAT THIS ADDS OVER scripts/ingest_dev.py
-----------------------------------------
A frontier that is a database query rather than a filesystem walk, so an
interrupted run resumes; leases, so a crashed worker's articles come back;
batched commits with a savepoint per article, which the 2026-09-10 benchmark
measured at 3.45x; and a refusal to ingest anything the quality contract
rejected.
"""
