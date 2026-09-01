"""images.json, the local images/ directory, and their link to content blocks."""

import re

from conftest import (ARTICLE_URL, JPEG_IMAGE, JPEG_SIZE, WEBP_IMAGE, WEBP_SIZE,
                      html_document, load, make_wacz)

BODY = """
<div class="block-content">
<p>Egy elegge hosszu bekezdes, hogy a readability ezt a blokkot valassza ki
   a cikk torzsekent, es ne valami mast a lap szelerol.</p>
<figure>
  <img src="https://cdn.example.com/kep.jpg" alt="A kep alt szovege">
  <figcaption>A kep alairasa</figcaption>
</figure>
<p>Egy masodik bekezdes, szinten eleg hosszu ahhoz, hogy szamitson.</p>
</div>
"""


def run(tmp_path, records):
    from causalia_extractor.pipeline import extract
    wacz = make_wacz(tmp_path / "p" / "page.wacz", records=records)
    result = extract(wacz, tmp_path / "out")
    assert result.output_dir is not None, result.error
    return result.output_dir


class TestExtraction:
    def test_an_archived_image_is_written_and_described(self, tmp_path):
        directory = run(tmp_path, [
            {"uri": ARTICLE_URL, "content_type": "text/html",
             "body": html_document(BODY)},
            {"uri": "https://cdn.example.com/kep.jpg",
             "content_type": "image/jpeg", "body": JPEG_IMAGE}])
        record = load(directory, "images.json")[0]
        assert record["id"] == "image_001"
        assert record["filename"] == "images/image_001.jpg"
        assert record["original_url"] == "https://cdn.example.com/kep.jpg"
        assert record["alt"] == "A kep alt szovege"
        assert record["caption"] == "A kep alairasa"
        assert (record["width"], record["height"]) == JPEG_SIZE
        assert record["mime_type"] == "image/jpeg"
        assert record["image_available"] is True
        assert (directory / record["filename"]).is_file()

    def test_the_content_type_decides_the_extension_not_the_url(self, tmp_path):
        # ripost.hu's CDN serves webp from extensionless URLs; going by the path
        # would file them as .bin.
        body = BODY.replace("https://cdn.example.com/kep.jpg",
                            "https://cdn.ripost.hu/2022/01/abc123XYZ")
        directory = run(tmp_path, [
            {"uri": ARTICLE_URL, "content_type": "text/html",
             "body": html_document(body)},
            {"uri": "https://cdn.ripost.hu/2022/01/abc123XYZ",
             "content_type": "image/webp", "body": WEBP_IMAGE}])
        record = load(directory, "images.json")[0]
        assert record["filename"].endswith(".webp")
        assert (record["width"], record["height"]) == WEBP_SIZE

    def test_ids_are_deterministic_and_in_document_order(self, tmp_path):
        body = BODY.replace("</div>",
                            '<figure><img src="https://cdn.example.com/masodik.jpg"'
                            ' alt="masodik"></figure></div>')
        directory = run(tmp_path, [
            {"uri": ARTICLE_URL, "content_type": "text/html",
             "body": html_document(body)},
            {"uri": "https://cdn.example.com/kep.jpg",
             "content_type": "image/jpeg", "body": JPEG_IMAGE},
            {"uri": "https://cdn.example.com/masodik.jpg",
             "content_type": "image/webp", "body": WEBP_IMAGE}])
        records = load(directory, "images.json")
        assert [r["id"] for r in records] == ["image_001", "image_002"]
        assert records[0]["original_url"].endswith("kep.jpg")


class TestMissingImages:
    def test_an_image_absent_from_the_capture_is_recorded_as_unavailable(
            self, tmp_path):
        directory = run(tmp_path, [
            {"uri": ARTICLE_URL, "content_type": "text/html",
             "body": html_document(BODY)}])
        record = load(directory, "images.json")[0]
        assert record["image_available"] is False
        assert record["original_url"] == "https://cdn.example.com/kep.jpg"

    def test_an_uncaptured_image_never_becomes_a_live_src(self, tmp_path):
        # Setting src to the publisher URL renders as a real network request to
        # their CDN every time the offline page is opened.
        directory = run(tmp_path, [
            {"uri": ARTICLE_URL, "content_type": "text/html",
             "body": html_document(BODY)}])
        html = (directory / "readability.html").read_text(encoding="utf-8")
        # The URL may appear as data-original-src - that is a record, not a
        # fetch. What must not exist is a bare src pointing off-site.
        assert not re.search(r'(?<![-\w])src="https?://', html)
        assert "data-archive-missing" in html


class TestRemovedFields:
    def test_position_block_id_and_size_bytes_are_gone(self, tmp_path):
        directory = run(tmp_path, [
            {"uri": ARTICLE_URL, "content_type": "text/html",
             "body": html_document(BODY)},
            {"uri": "https://cdn.example.com/kep.jpg",
             "content_type": "image/jpeg", "body": JPEG_IMAGE}])
        record = load(directory, "images.json")[0]
        for absent in ("position", "block_id", "size_bytes"):
            assert absent not in record


class TestLinkToContent:
    def test_every_image_block_resolves_to_a_record_and_a_file(self, tmp_path):
        directory = run(tmp_path, [
            {"uri": ARTICLE_URL, "content_type": "text/html",
             "body": html_document(BODY)},
            {"uri": "https://cdn.example.com/kep.jpg",
             "content_type": "image/jpeg", "body": JPEG_IMAGE}])
        by_id = {r["id"]: r for r in load(directory, "images.json")}
        blocks = [b for b in load(directory, "content.json")["blocks"]
                  if b["type"] == "image"]
        assert blocks
        for block in blocks:
            record = by_id[block["image_id"]]
            assert record["image_available"]
            assert (directory / record["filename"]).is_file()
