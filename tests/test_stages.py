"""The stage split: screenshots can be switched off, and filled in later.

Screenshots are strategically required and are never removed from the
architecture. They are also 4.826 MB of the 5.353 MB an article writes (90.2%)
and ~1.48x of sustained throughput, so an urgent run must be able to defer them
and a backfill must be able to add them without re-reading the article.

The invariant these tests defend: switching the stage off changes what is
WRITTEN and nothing else - not the article, not its quality verdict, and above
all not an existing screenshot.
"""

from __future__ import annotations

import json

from conftest import ARTICLE_URL, PNG_IMAGE, html_document, make_wacz
from test_pipeline import simple_wacz
from causalia_extractor.pipeline import extract, extract_screenshot


def with_screenshot(tmp_path):
    return make_wacz(tmp_path / "cap" / "page.wacz", records=[
        {"uri": ARTICLE_URL, "content_type": "text/html",
         "body": html_document("<p>Egy rendes bekezdés, elég hosszú ahhoz, "
                               "hogy a readability megtartsa.</p>")},
        {"uri": f"urn:fullPage:{ARTICLE_URL}", "type": "resource",
         "content_type": "image/png", "body": PNG_IMAGE}])


def without_screenshot(tmp_path, name="plain"):
    """A capture Browsertrix took before --screenshot was enabled."""
    return make_wacz(tmp_path / name / "page.wacz", records=[
        {"uri": ARTICLE_URL, "content_type": "text/html",
         "body": html_document("<p>Egy rendes bekezdés, elég hosszú ahhoz, "
                               "hogy a readability megtartsa.</p>")}])


def marker(directory):
    return json.loads((directory / "extraction.json").read_text(encoding="utf-8"))


class TestContentOnly:
    def test_content_only_writes_no_screenshot(self, tmp_path):
        result = extract(with_screenshot(tmp_path), tmp_path / "out",
                         stages=("content",))
        assert not list(result.output_dir.glob("screenshot.*"))
        assert result.screenshot_state == "skipped"

    def test_content_only_does_not_make_the_article_partial(self, tmp_path):
        """The trap: the 'no screenshot' warning would demote EVERY article to
        partial_valid when the stage is simply switched off. It belongs to the
        case where we looked and the capture had none."""
        plain = simple_wacz(tmp_path)
        with_stage = extract(plain, tmp_path / "a", stages=("content", "screenshot"))
        without = extract(plain, tmp_path / "b", stages=("content",))
        assert without.screenshot_state == "skipped"
        assert not any("screenshot" in w for w in without.warnings)
        assert without.quality == "success"
        # and the article itself is identical either way
        assert without.counts == with_stage.counts

    def test_a_capture_with_no_screenshot_says_so_when_we_looked(self, tmp_path):
        result = extract(without_screenshot(tmp_path), tmp_path / "out",
                         stages=("content", "screenshot"))
        assert result.screenshot_state == "none_in_archive"
        assert any("screenshot" in w for w in result.warnings)

    def test_content_only_never_touches_an_existing_screenshot(self, tmp_path):
        """The archival invariant. An urgent content-only re-run must not undo
        a screenshot an earlier pass already captured."""
        wacz = with_screenshot(tmp_path)
        directory = extract(wacz, tmp_path / "out").output_dir
        before = (directory / "screenshot.png").read_bytes()

        extract(wacz, tmp_path / "out", stages=("content",), force=True)
        assert (directory / "screenshot.png").read_bytes() == before


class TestTheBackfill:
    def test_a_screenshot_only_pass_fills_in_what_content_only_skipped(self, tmp_path):
        wacz = with_screenshot(tmp_path)
        directory = extract(wacz, tmp_path / "out", stages=("content",)).output_dir
        assert not list(directory.glob("screenshot.*"))

        result = extract_screenshot(wacz, tmp_path / "out")
        assert result.state == "present"
        assert result.output_dir == directory
        assert (directory / "screenshot.png").read_bytes() == PNG_IMAGE

    def test_the_backfill_leaves_the_commit_marker_alone(self, tmp_path):
        """extraction.json describes the CONTENT extraction. A screenshot
        arriving later is a separate fact, recorded in the ledger and in
        article_artifact - rewriting the marker would also let a backfill race
        a content extraction into a half-written file."""
        wacz = with_screenshot(tmp_path)
        directory = extract(wacz, tmp_path / "out", stages=("content",)).output_dir
        before = marker(directory)

        extract_screenshot(wacz, tmp_path / "out")
        assert marker(directory) == before

    def test_the_backfill_creates_no_article_directory_of_its_own(self, tmp_path):
        """It writes into the article's existing directory, addressed by the
        same identity the content extraction used."""
        wacz = with_screenshot(tmp_path)
        content = extract(wacz, tmp_path / "out", stages=("content",))
        shot = extract_screenshot(wacz, tmp_path / "out")
        assert shot.output_dir == content.output_dir

    def test_a_capture_with_no_screenshot_is_not_a_failure(self, tmp_path):
        """`none_in_archive` is normal for everything crawled before
        2026-08-12 and must not be retried on every backfill."""
        result = extract_screenshot(without_screenshot(tmp_path), tmp_path / "out")
        assert result.state == "none_in_archive"
        assert result.error is None

    def test_an_unreadable_archive_is_reported_not_raised(self, tmp_path):
        broken = tmp_path / "broken.wacz"
        broken.write_bytes(b"not a zip at all")
        result = extract_screenshot(broken, tmp_path / "out")
        assert result.state == "failed"
        assert result.error


class TestWhichScreenshotIsRecorded:
    def test_a_browsertrix_capture_beats_a_webp_sidecar(self, tmp_path):
        """The previous code took sorted(glob('screenshot.*'))[-1], so
        ALPHABETICAL ORDER decided - and .webp sorts last, which is exactly the
        wrong way round."""
        import sys
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "scripts"))
        from cx_ingest import find_screenshot

        directory = tmp_path / "article"
        directory.mkdir()
        (directory / "screenshot.webp").write_bytes(b"backfill sidecar")
        (directory / "screenshot.png").write_bytes(PNG_IMAGE)
        assert find_screenshot(directory).name == "screenshot.png"

    def test_a_lone_sidecar_is_still_recorded(self, tmp_path):
        import sys
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "scripts"))
        from cx_ingest import find_screenshot

        directory = tmp_path / "article"
        directory.mkdir()
        (directory / "screenshot.webp").write_bytes(b"backfill sidecar")
        assert find_screenshot(directory).name == "screenshot.webp"
