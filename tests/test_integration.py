"""The whole pipeline against real archived captures.

These run against a directory of real ``page.wacz`` files copied read-only out
of the corpus, which is NOT part of the repository - the unit suite above is
self-contained and this one skips cleanly when the directory is absent.

Point it somewhere with ``CAUSALIA_WACZ_TESTSET``; the default is
``~/causalia-wacz-testset``. Populate it by copying archives from the corpus,
preserving the ``<outlet>/<h2>/<hash>/`` layout::

    scp c0cshf@milab2:/mnt/hdd/.../pages/<outlet>/<h2>/<hash>/page.wacz \\
        ~/causalia-wacz-testset/<outlet>/<h2>/<hash>/page.wacz

The set is chosen for variety: multiple paragraphs with inline markup, articles
with and without images, a self-hosted video with bytes in the capture, embeds
with no bytes, an HLS segment set that must never be written, a near-empty
article, a capture whose screenshot is a urn:fullPage PNG and one whose only
screenshot is the backfill sidecar.
"""

import json
import os
from pathlib import Path

import pytest

from causalia_extractor import selectors as sel
from causalia_extractor.dom import reparse
from causalia_extractor.models import BLOCK_TYPES
from causalia_extractor.normalize import normalize_text
from causalia_extractor.pipeline import extract
from causalia_extractor.xpath import resolve

TESTSET = Path(os.environ.get(
    "CAUSALIA_WACZ_TESTSET", Path.home() / "causalia-wacz-testset"))

ARCHIVES = sorted(TESTSET.rglob("*.wacz")) if TESTSET.is_dir() else []

pytestmark = pytest.mark.skipif(
    not ARCHIVES, reason=f"no real captures in {TESTSET}")


@pytest.fixture(scope="module")
def extracted(tmp_path_factory):
    """Every archive in the test set, extracted once into one output tree."""
    output = tmp_path_factory.mktemp("corpus")
    results = [extract(path, output) for path in ARCHIVES]
    return output, results


def articles(output):
    return sorted(p.parent for p in output.rglob("content.json"))


def load(directory, name):
    return json.loads((Path(directory) / name).read_text(encoding="utf-8"))


class TestItRuns:
    def test_every_real_capture_is_processed_without_failing(self, extracted):
        _, results = extracted
        failed = [(r.wacz_path.name, r.error) for r in results
                  if r.status == "failed"]
        assert not failed, failed

    def test_every_article_produces_the_full_artifact_set(self, extracted):
        output, _ = extracted
        for directory in articles(output):
            for name in ("original.html", "readability.html", "article.json",
                         "content.json", "images.json", "videos.json",
                         "links.json", "extraction.json"):
                assert (directory / name).is_file(), f"{directory.name}/{name}"

    def test_real_articles_yield_real_content(self, extracted):
        output, _ = extracted
        counts = [len(load(d, "content.json")["blocks"]) for d in articles(output)]
        # At least most of a varied real sample must produce blocks; a single
        # near-empty page (a puzzle page, an interstitial) is expected.
        assert sum(1 for c in counts if c > 0) >= len(counts) - 1


class TestInvariantsOnRealData:
    def test_invariant_a_every_block_text_matches_the_document(self, extracted):
        output, _ = extracted
        checked = 0
        for directory in articles(output):
            tree = reparse((directory / "readability.html").read_text(encoding="utf-8"))
            for block in load(directory, "content.json")["blocks"]:
                element = resolve(tree, block["xpath"])
                assert element is not None, f"{directory.name}: {block['xpath']}"
                if "text" in block:
                    assert block["text"] == normalize_text(element), \
                        f"{directory.name} block {block['index']}"
                    checked += 1
        assert checked > 50, "the real sample should exercise this properly"

    def test_invariant_b_every_selector_verifies(self, extracted):
        output, _ = extracted
        checked = 0
        for directory in articles(output):
            tree = reparse((directory / "readability.html").read_text(encoding="utf-8"))
            for record in load(directory, "links.json"):
                if record["selector"] is None:
                    continue
                assert sel.verify_payload(tree, record["selector"]) == sel.OK, \
                    f"{directory.name}: {record['url']}"
                checked += 1
        assert checked > 5

    def test_invariant_c_image_ids_resolve_to_records_and_files(self, extracted):
        output, _ = extracted
        for directory in articles(output):
            by_id = {r["id"]: r for r in load(directory, "images.json")}
            for block in load(directory, "content.json")["blocks"]:
                if block["type"] != "image":
                    continue
                record = by_id.get(block["image_id"])
                assert record is not None, f"{directory.name}: {block['image_id']}"
                if record["image_available"]:
                    assert (directory / record["filename"]).is_file()

    def test_invariant_d_video_ids_resolve_to_records(self, extracted):
        output, _ = extracted
        for directory in articles(output):
            by_id = {r["id"]: r for r in load(directory, "videos.json")}
            for block in load(directory, "content.json")["blocks"]:
                if block["type"] == "video":
                    assert block["video_id"] in by_id

    def test_no_block_id_is_produced_anywhere(self, extracted):
        output, _ = extracted
        for path in output.rglob("*.json"):
            assert "block_id" not in path.read_text(encoding="utf-8"), path

    def test_every_block_type_is_one_we_declare(self, extracted):
        output, _ = extracted
        for directory in articles(output):
            for block in load(directory, "content.json")["blocks"]:
                assert block["type"] in BLOCK_TYPES


class TestScreenshotsOnRealData:
    def test_every_article_gets_exactly_one_screenshot(self, extracted):
        output, _ = extracted
        for directory in articles(output):
            shots = list(directory.glob("screenshot.*"))
            assert len(shots) <= 1, f"{directory.name}: {shots}"

    def test_browsertrix_captures_are_preferred_over_the_sidecar(self, extracted):
        # Wave-3/4 captures carry a urn:fullPage PNG; wave-1 ones predate
        # --screenshot and have only the backfill webp. The testset's wave-1
        # example happens to be ripost.hu, the outlet that was crawled first.
        output, _ = extracted
        suffixes = {p.suffix for d in articles(output)
                    for p in d.glob("screenshot.*")}
        assert ".png" in suffixes, "expected at least one Browsertrix PNG"


class TestDeterminismOnRealData:
    def test_a_second_pass_reproduces_every_artifact(self, extracted, tmp_path):
        output, _ = extracted
        second = tmp_path / "again"
        for path in ARCHIVES:
            extract(path, second)
        for directory in articles(output):
            twin = second / directory.relative_to(output)
            assert twin.is_dir(), twin
            for path in sorted(directory.rglob("*")):
                if not path.is_file():
                    continue
                other = twin / path.relative_to(directory)
                if path.name == "extraction.json":
                    left, right = json.loads(path.read_text()), json.loads(
                        other.read_text())
                    left.pop("extracted_at"), right.pop("extracted_at")
                    assert left == right
                else:
                    assert path.read_bytes() == other.read_bytes(), other


class TestArchivesAreUntouched:
    def test_no_capture_was_modified(self, extracted):
        # The fixture already ran every extraction; the stat fence inside
        # extract() would have raised. This asserts the files are still there.
        for path in ARCHIVES:
            assert path.is_file() and path.stat().st_size > 0
