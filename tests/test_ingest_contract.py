"""The ingestion path's refusals, with no database in sight.

Everything here is about what the loader must NOT accept. The three refusals
each correspond to a defect that was measured or read out of the code on
2026-09-11:

  IncompleteExtraction  a worker killed between content.json and the commit
                        marker left a directory the old walker accepted and the
                        loader then died on - 5 of 46,253 on the benchmark output
  UnusableExtraction    an article with no prose block was recorded `partial`,
                        ingested, and answered searches on its metadata alone
  CaptureNotFound       the old seed_crawler_rows INSERTed a fabricated
                        public.archives row rather than admitting it had none
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from cx_ingest import (                                          # noqa: E402
    CaptureNotFound, IncompleteExtraction, REQUIRED_ARTIFACTS,
    UnusableExtraction, ingest, read_marker, resolve_capture, seed_crawler_rows)


def write_article(directory: Path, *, quality="success", artifacts=None,
                  omit=(), blocks=None):
    """A minimal article directory shaped exactly as the extractor writes one."""
    directory.mkdir(parents=True, exist_ok=True)
    blocks = [{"type": "paragraph", "index": 1, "xpath": "/html/body/p",
               "text": "Egy bekezdés."}] if blocks is None else blocks
    payloads = {
        "article.json": {"archive_id": "a" * 64, "outlet": "mandiner.hu",
                         "source_url": "https://mandiner.hu/x",
                         "canonical_url": "https://mandiner.hu/x",
                         "title": "Cím", "subtitle": None, "description": None,
                         "author": [], "publisher": None, "section": None,
                         "language": "hu", "tags": [], "published_at": None,
                         "updated_at": None, "captured_at": None},
        "content.json": {"blocks": blocks},
        "images.json": [], "videos.json": [], "links.json": [],
    }
    written = []
    for name, payload in payloads.items():
        if name in omit:
            continue
        (directory / name).write_text(json.dumps(payload), encoding="utf-8")
        written.append(name)
    if "extraction.json" not in omit:
        (directory / "extraction.json").write_text(json.dumps({
            "extraction_version": "causalia-article-extractor/2.0.0",
            "extracted_at": "2026-09-11T00:00:00Z",
            "extraction_status": "success",
            "quality": quality,
            "quality_reasons": [],
            "artifacts": written if artifacts is None else artifacts,
            "error": None,
        }), encoding="utf-8")
    return directory


class TestTheCommitMarker:
    def test_a_complete_directory_is_accepted(self, tmp_path):
        payload = read_marker(write_article(tmp_path / "a"))
        assert payload["quality"] == "success"

    def test_a_directory_without_the_marker_is_refused(self, tmp_path):
        """The exact state a killed worker leaves: every artifact but the last."""
        directory = write_article(tmp_path / "a", omit=("extraction.json",))
        assert (directory / "content.json").is_file(), "the old walker's key"
        with pytest.raises(IncompleteExtraction):
            read_marker(directory)

    def test_a_marker_promising_a_missing_artifact_is_refused(self, tmp_path):
        directory = write_article(tmp_path / "a",
                                  artifacts=["article.json", "images/image_001.jpg"])
        with pytest.raises(IncompleteExtraction, match="image_001"):
            read_marker(directory)

    @pytest.mark.parametrize("missing", REQUIRED_ARTIFACTS)
    def test_every_required_artifact_is_actually_required(self, tmp_path, missing):
        """Kill the worker after each artifact in turn; none of them loads."""
        directory = write_article(tmp_path / missing.replace("/", "_"),
                                  omit=(missing,), artifacts=[])
        with pytest.raises(IncompleteExtraction):
            read_marker(directory)

    def test_a_marker_from_before_the_quality_contract_is_refused(self, tmp_path):
        directory = tmp_path / "old"
        write_article(directory)
        (directory / "extraction.json").write_text(json.dumps({
            "extraction_version": "causalia-article-extractor/2.0.0",
            "extracted_at": "2026-09-01T00:00:00Z",
            "extraction_status": "partial"}), encoding="utf-8")
        with pytest.raises(IncompleteExtraction, match="quality contract"):
            read_marker(directory)

    def test_an_invalid_extraction_is_refused(self, tmp_path):
        directory = write_article(tmp_path / "a", quality="invalid")
        with pytest.raises(UnusableExtraction):
            read_marker(directory)

    def test_a_partial_valid_extraction_is_accepted(self, tmp_path):
        """partial_valid means the ARTICLE is usable - a missing author is not
        a reason to lose an article, and 66.8% of mandiner has none."""
        assert read_marker(write_article(tmp_path / "a", quality="partial_valid"))


class FakeCursor:
    """Records every statement and answers RETURNING with an incrementing id."""

    def __init__(self, capture_row=(7, "sha")):
        self.statements: list[str] = []
        self.capture_row = capture_row
        self._next = 100

    def execute(self, sql, params=None):
        self.statements.append(" ".join(sql.split()))

    def fetchone(self):
        if self.statements and "FROM archives" in self.statements[-1]:
            return self.capture_row
        self._next += 1
        return (self._next, None)

    def fetchall(self):
        return []

    @property
    def sql(self) -> str:
        return "\n".join(self.statements)


class TestIngestionWritesNothingOutsideCorpus:
    def test_ingest_never_inserts_into_the_crawlers_tables(self, tmp_path):
        """The P0 defect: seed_crawler_rows INSERTed a urls row and a fresh
        archives row per article per run, fabricating capture history."""
        cur = FakeCursor()
        ingest(cur, write_article(tmp_path / "a"))
        assert "INSERT INTO urls" not in cur.sql
        assert "INSERT INTO archives" not in cur.sql
        for statement in cur.statements:
            if statement.startswith(("INSERT", "UPDATE", "DELETE")):
                assert "corpus." in statement, statement[:120]

    def test_ingest_reads_the_capture_instead_of_inventing_one(self, tmp_path):
        cur = FakeCursor()
        ingest(cur, write_article(tmp_path / "a"))
        assert "FROM archives" in cur.sql
        assert "status = 'success'" in cur.sql

    def test_a_supplied_capture_skips_the_lookup(self, tmp_path):
        cur = FakeCursor()
        ingest(cur, write_article(tmp_path / "a"), capture=(42, "sha256"))
        assert "FROM archives" not in cur.sql

    def test_a_missing_capture_is_refused_not_invented(self, tmp_path):
        class Empty(FakeCursor):
            def fetchone(self):
                if "FROM archives" in self.statements[-1]:
                    return None
                return super().fetchone()

        with pytest.raises(CaptureNotFound):
            ingest(Empty(), write_article(tmp_path / "a"))

    def test_the_extraction_row_carries_the_prose_block_count(self, tmp_path):
        cur = FakeCursor()
        ingest(cur, write_article(tmp_path / "a", blocks=[
            {"type": "paragraph", "index": 1, "xpath": "/p[1]", "text": "Egy."},
            {"type": "image", "index": 2, "xpath": "/img", "image_id": None},
            {"type": "paragraph", "index": 3, "xpath": "/p[2]", "text": "  "},
        ]))
        assert "content_block_count" in cur.sql

    def test_an_invalid_extraction_never_reaches_the_database(self, tmp_path):
        cur = FakeCursor()
        with pytest.raises(UnusableExtraction):
            ingest(cur, write_article(tmp_path / "a", quality="invalid"))
        assert not cur.statements, "nothing may be written before the refusal"


class TestTheDevelopmentSeedIsFenced:
    def test_seeding_refuses_unless_the_caller_says_it_is_development(self, tmp_path):
        with pytest.raises(RuntimeError, match="production"):
            seed_crawler_rows(FakeCursor(), tmp_path,
                              {"archive_id": "a" * 64, "outlet": "x.hu",
                               "source_url": "https://x.hu/1"})
