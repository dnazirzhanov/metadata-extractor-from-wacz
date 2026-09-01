"""Reading a Browsertrix .wacz."""

import zipfile

import pytest

from causalia_extractor.wacz import (
    ArchiveUnreadable, decode_html, find_backfilled_screenshot, read_archive,
    read_archive_for_page)
from conftest import ARTICLE_URL, JPEG_IMAGE, PNG_IMAGE, make_wacz


class TestOpening:
    def test_a_well_formed_capture_yields_the_article(self, tmp_path):
        path = make_wacz(tmp_path / "page.wacz", records=[
            {"uri": ARTICLE_URL, "content_type": "text/html",
             "body": "<html><body><p>szoveg</p></body></html>"}])
        contents = read_archive_for_page(path)
        assert contents.page_url == ARTICLE_URL
        assert b"szoveg" in contents.main_html
        assert contents.captured_at == "2026-08-03T08:50:32.956Z"

    def test_a_truncated_zip_raises_rather_than_returning_nothing(self, tmp_path):
        path = tmp_path / "broken.wacz"
        path.write_bytes(b"PK\x03\x04 this is not a zip")
        with pytest.raises(ArchiveUnreadable):
            read_archive(path)

    def test_a_missing_file_raises(self, tmp_path):
        with pytest.raises(ArchiveUnreadable):
            read_archive(tmp_path / "absent.wacz")

    def test_a_capture_with_no_html_record_yields_no_document(self, tmp_path):
        path = make_wacz(tmp_path / "page.wacz", records=[
            {"uri": "https://cdn.example.com/x.jpg",
             "content_type": "image/jpeg", "body": JPEG_IMAGE}])
        assert read_archive_for_page(path).main_html is None

    def test_the_warc_member_name_is_globbed_not_assumed(self, tmp_path):
        # Per-page archives use archive/page.warc.gz; crawl-level ones use
        # archive/data.warc.gz.
        from conftest import make_warc
        path = tmp_path / "page.wacz"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("archive/data.warc.gz", make_warc([
                {"uri": ARTICLE_URL, "content_type": "text/html",
                 "body": "<html><body><p>x</p></body></html>"}]))
        assert read_archive(path).main_html is not None


class TestChoosingTheArticleDocument:
    def test_a_redirect_stub_never_wins_over_its_target(self, tmp_path):
        # Measured upstream on origo.hu: the seed 301s with a 1KB body while
        # the article sits at the target with 105KB. Taking the exact URL match
        # hands Readability an empty document.
        target = "https://origo.hu/sport/f1/2025/11/baleset"
        path = make_wacz(tmp_path / "page.wacz", page_url=ARTICLE_URL, records=[
            {"uri": ARTICLE_URL, "content_type": "text/html", "status": "301 Moved",
             "headers": {"Location": target}, "body": "<html>stub</html>"},
            {"uri": target, "content_type": "text/html",
             "body": "<html><body><p>a valodi cikk</p></body></html>"}])
        contents = read_archive_for_page(path)
        assert b"valodi cikk" in contents.main_html
        assert contents.main_url == target

    def test_several_html_responses_are_all_counted(self, tmp_path):
        path = make_wacz(tmp_path / "page.wacz", records=[
            {"uri": ARTICLE_URL, "content_type": "text/html",
             "body": "<html><body><p>cikk</p></body></html>"},
            {"uri": "https://ripost.hu/ad-frame", "content_type": "text/html",
             "body": "<html><body>hirdetes</body></html>"}])
        assert read_archive_for_page(path).html_record_count == 2


class TestPayloads:
    def test_images_and_video_are_kept(self, tmp_path):
        path = make_wacz(tmp_path / "page.wacz", records=[
            {"uri": ARTICLE_URL, "content_type": "text/html", "body": "<html></html>"},
            {"uri": "https://cdn.example.com/a.jpg",
             "content_type": "image/jpeg", "body": JPEG_IMAGE}])
        payloads = read_archive_for_page(path).payloads
        assert "https://cdn.example.com/a.jpg" in payloads

    def test_stylesheets_and_scripts_are_never_buffered(self, tmp_path):
        # On a 6 MB capture these are most of the bytes and we only discard them.
        path = make_wacz(tmp_path / "page.wacz", records=[
            {"uri": ARTICLE_URL, "content_type": "text/html", "body": "<html></html>"},
            {"uri": "https://cdn.example.com/app.css",
             "content_type": "text/css", "body": "body{}"},
            {"uri": "https://cdn.example.com/app.js",
             "content_type": "application/javascript", "body": "var x=1"}])
        assert read_archive_for_page(path).payloads == {}


class TestScreenshots:
    def test_a_urn_fullpage_resource_is_found(self, tmp_path):
        # The word "screenshot" appears nowhere in the URI; the variant name is
        # the only handle.
        path = make_wacz(tmp_path / "page.wacz", records=[
            {"uri": ARTICLE_URL, "content_type": "text/html", "body": "<html></html>"},
            {"uri": f"urn:fullPage:{ARTICLE_URL}", "type": "resource",
             "content_type": "image/png", "body": PNG_IMAGE}])
        contents = read_archive_for_page(path)
        assert contents.screenshot == PNG_IMAGE
        assert contents.screenshot_source == "fullPage"
        assert contents.screenshot_ext == ".png"

    def test_the_better_variant_wins_whatever_the_order(self, tmp_path):
        path = make_wacz(tmp_path / "page.wacz", records=[
            {"uri": ARTICLE_URL, "content_type": "text/html", "body": "<html></html>"},
            {"uri": f"urn:thumbnail:{ARTICLE_URL}", "type": "resource",
             "content_type": "image/png", "body": b"thumbnail-bytes"},
            {"uri": f"urn:fullPage:{ARTICLE_URL}", "type": "resource",
             "content_type": "image/png", "body": PNG_IMAGE}])
        assert read_archive_for_page(path).screenshot_source == "fullPage"

    def test_a_non_screenshot_urn_record_is_ignored(self, tmp_path):
        path = make_wacz(tmp_path / "page.wacz", records=[
            {"uri": ARTICLE_URL, "content_type": "text/html", "body": "<html></html>"},
            {"uri": f"urn:pageinfo:{ARTICLE_URL}", "type": "resource",
             "content_type": "application/json", "body": b'{"a":1}'}])
        assert read_archive_for_page(path).screenshot is None

    def test_a_sidecar_beside_the_archive_is_found(self, tmp_path):
        path = make_wacz(tmp_path / "page.wacz", records=[
            {"uri": ARTICLE_URL, "content_type": "text/html", "body": "<html></html>"}])
        (tmp_path / "screenshot.webp").write_bytes(b"webp-bytes")
        assert find_backfilled_screenshot(path).name == "screenshot.webp"

    def test_no_sidecar_is_not_an_error(self, tmp_path):
        path = make_wacz(tmp_path / "page.wacz", records=[
            {"uri": ARTICLE_URL, "content_type": "text/html", "body": "<html></html>"}])
        assert find_backfilled_screenshot(path) is None


class TestDecoding:
    def test_the_declared_charset_is_honoured(self, tmp_path):
        # A handful of older magyarnemzet archive pages were served as
        # iso-8859-2; decoding those as utf-8 destroys every long vowel.
        body = "<html><body><p>Arvizturo tukorfurogep</p></body></html>".encode("iso-8859-2")
        path = make_wacz(tmp_path / "page.wacz", records=[
            {"uri": ARTICLE_URL, "content_type": "text/html; charset=iso-8859-2",
             "body": body}])
        assert "Arvizturo" in decode_html(read_archive_for_page(path))

    def test_undecodable_bytes_do_not_crash_the_read(self, tmp_path):
        path = make_wacz(tmp_path / "page.wacz", records=[
            {"uri": ARTICLE_URL, "content_type": "text/html",
             "body": b"<html><body>\xff\xfe not utf-8</body></html>"}])
        assert "not utf-8" in decode_html(read_archive_for_page(path))


class TestPagesJsonl:
    def test_an_exotic_line_separator_in_a_title_does_not_shred_the_entry(
            self, tmp_path):
        # str.splitlines() breaks on U+0085 and U+2028, which occur in real
        # article titles in this corpus.
        title = "Cim" + chr(0x2028) + "folytatas"
        path = make_wacz(tmp_path / "page.wacz", page_title=title, records=[
            {"uri": ARTICLE_URL, "content_type": "text/html", "body": "<html></html>"}])
        assert read_archive_for_page(path).page_entry["title"] == title

    def test_a_capture_with_no_pages_jsonl_still_reads(self, tmp_path):
        path = make_wacz(tmp_path / "page.wacz", include_pages=False, records=[
            {"uri": ARTICLE_URL, "content_type": "text/html",
             "body": "<html><body><p>x</p></body></html>"}])
        contents = read_archive_for_page(path)
        assert contents.page_url is None
        assert contents.main_url == ARTICLE_URL
