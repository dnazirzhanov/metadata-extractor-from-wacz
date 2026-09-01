"""Screenshot discovery and selection."""

from conftest import ARTICLE_URL, JPEG_IMAGE, PNG_IMAGE, html_document, make_wacz
from causalia_extractor import screenshots
from causalia_extractor.wacz import read_archive_for_page


def choose(tmp_path, records, sidecar=None):
    wacz = make_wacz(tmp_path / "p" / "page.wacz", records=records)
    if sidecar:
        (wacz.parent / sidecar).write_bytes(b"sidecar-bytes")
    return screenshots.choose(read_archive_for_page(wacz), wacz)


HTML = {"uri": ARTICLE_URL, "content_type": "text/html",
        "body": html_document("<p>x</p>")}


class TestPreference:
    def test_a_browsertrix_png_is_preferred_over_the_backfill_sidecar(self, tmp_path):
        choice = choose(tmp_path, [HTML, {
            "uri": f"urn:fullPage:{ARTICLE_URL}", "type": "resource",
            "content_type": "image/png", "body": PNG_IMAGE}],
            sidecar="screenshot.webp")
        assert choice.from_browsertrix
        assert choice.source == "fullPage"
        assert choice.filename == "screenshot.png"
        assert choice.body == PNG_IMAGE

    def test_a_browsertrix_jpeg_is_preferred_too(self, tmp_path):
        choice = choose(tmp_path, [HTML, {
            "uri": f"urn:fullPage:{ARTICLE_URL}", "type": "resource",
            "content_type": "image/jpeg", "body": JPEG_IMAGE}],
            sidecar="screenshot.webp")
        assert choice.from_browsertrix
        assert choice.filename == "screenshot.jpg"

    def test_the_final_full_page_beats_the_in_progress_one(self, tmp_path):
        choice = choose(tmp_path, [HTML,
            {"uri": f"urn:fullPage:{ARTICLE_URL}", "type": "resource",
             "content_type": "image/png", "body": PNG_IMAGE},
            {"uri": f"urn:fullPageFinal:{ARTICLE_URL}", "type": "resource",
             "content_type": "image/png", "body": PNG_IMAGE}])
        assert choice.source == "fullPageFinal"

    def test_the_full_page_beats_the_viewport(self, tmp_path):
        choice = choose(tmp_path, [HTML,
            {"uri": f"urn:view:{ARTICLE_URL}", "type": "resource",
             "content_type": "image/png", "body": PNG_IMAGE},
            {"uri": f"urn:fullPage:{ARTICLE_URL}", "type": "resource",
             "content_type": "image/png", "body": PNG_IMAGE}])
        assert choice.source == "fullPage"


class TestFallback:
    def test_the_sidecar_is_used_only_when_the_archive_has_none(self, tmp_path):
        # ripost.hu's crawl predates --screenshot, so for that outlet a sidecar
        # is the normal case and not a defect.
        choice = choose(tmp_path, [HTML], sidecar="screenshot.webp")
        assert choice is not None
        assert not choice.from_browsertrix
        assert choice.source == "backfill"
        assert choice.filename == "screenshot.webp"

    def test_no_screenshot_anywhere_is_reported_as_absence(self, tmp_path):
        assert choose(tmp_path, [HTML]) is None


class TestWriting:
    def test_exactly_one_screenshot_is_written(self, tmp_path):
        from causalia_extractor.pipeline import extract
        wacz = make_wacz(tmp_path / "p" / "page.wacz", records=[HTML,
            {"uri": f"urn:fullPage:{ARTICLE_URL}", "type": "resource",
             "content_type": "image/png", "body": PNG_IMAGE}])
        (wacz.parent / "screenshot.webp").write_bytes(b"sidecar")
        directory = extract(wacz, tmp_path / "out").output_dir
        shots = sorted(p.name for p in directory.glob("screenshot.*"))
        assert shots == ["screenshot.png"]

    def test_a_missing_screenshot_makes_the_extraction_partial_not_failed(
            self, tmp_path):
        from causalia_extractor.pipeline import extract
        wacz = make_wacz(tmp_path / "p" / "page.wacz", records=[HTML])
        result = extract(wacz, tmp_path / "out")
        assert result.status == "partial"
        assert result.ok
