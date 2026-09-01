"""The invariants every artifact set must satisfy.

These are the contract. If one of these fails, stored evidence points somewhere
other than where it claims to, which is the failure this system exists to avoid.
"""

import json

import pytest

from causalia_extractor import selectors as sel
from causalia_extractor.normalize import normalize_text
from causalia_extractor.xpath import resolve
from conftest import (ARTICLE_BODY, ARTICLE_URL, JPEG_IMAGE, MP4_BODY, PNG_IMAGE,
                      html_document, load, make_wacz, reader_tree)

RICH_BODY = ARTICLE_BODY.replace(
    "</div>\n</div>",
    '<video src="https://video.example.hu/a.mp4"></video>'
    '<iframe src="https://www.youtube.com/embed/UwTYPHnSP8M"></iframe>'
    "</div>\n</div>")


@pytest.fixture
def article(tmp_path):
    from causalia_extractor.pipeline import extract
    wacz = make_wacz(tmp_path / "p" / "ripost.hu" / "aa" / ("a" * 64) / "page.wacz",
                     records=[
        {"uri": ARTICLE_URL, "content_type": "text/html",
         "body": html_document(RICH_BODY)},
        {"uri": "https://cdn.example.com/kep.jpg",
         "content_type": "image/jpeg", "body": JPEG_IMAGE},
        {"uri": "https://video.example.hu/a.mp4",
         "content_type": "video/mp4", "body": MP4_BODY},
        {"uri": f"urn:fullPage:{ARTICLE_URL}", "type": "resource",
         "content_type": "image/png", "body": PNG_IMAGE}])
    result = extract(wacz, tmp_path / "out")
    assert result.ok, result.error
    return result.output_dir


class TestInvariantA:
    """content_block.text == normalize_text(XPath(readability.html, xpath))"""

    def test_every_textual_block_matches_the_document(self, article):
        tree = reader_tree(article)
        blocks = load(article, "content.json")["blocks"]
        textual = [b for b in blocks if "text" in b]
        assert textual
        for block in textual:
            element = resolve(tree, block["xpath"])
            assert element is not None, block["xpath"]
            assert block["text"] == normalize_text(element)

    def test_every_list_item_matches_the_document(self, article):
        tree = reader_tree(article)
        for block in load(article, "content.json")["blocks"]:
            for item in block.get("items", []):
                element = resolve(tree, item["xpath"])
                assert element is not None
                assert item["text"] == normalize_text(element)


class TestInvariantB:
    """XPath -> element -> normalised text -> [start:end] == quote.exact"""

    def test_every_link_selector_verifies(self, article):
        tree = reader_tree(article)
        records = load(article, "links.json")
        assert records
        for record in records:
            assert sel.verify_payload(tree, record["selector"]) == sel.OK

    def test_a_tampered_selector_is_detected(self, article):
        tree = reader_tree(article)
        selector = load(article, "links.json")[0]["selector"]
        selector["refinedBy"]["start"] += 1
        assert sel.verify_payload(tree, selector) != sel.OK


class TestInvariantC:
    """Every image content block resolves to an image record."""

    def test_image_ids_resolve(self, article):
        by_id = {r["id"]: r for r in load(article, "images.json")}
        blocks = [b for b in load(article, "content.json")["blocks"]
                  if b["type"] == "image"]
        assert blocks
        for block in blocks:
            assert block["image_id"] in by_id

    def test_an_available_image_has_a_file_on_disk(self, article):
        for record in load(article, "images.json"):
            if record["image_available"]:
                assert (article / record["filename"]).is_file()


class TestInvariantD:
    """Every video content block resolves to a video record."""

    def test_video_ids_resolve(self, article):
        by_id = {r["id"]: r for r in load(article, "videos.json")}
        blocks = [b for b in load(article, "content.json")["blocks"]
                  if b["type"] == "video"]
        assert blocks
        for block in blocks:
            assert block["video_id"] in by_id

    def test_an_archived_video_has_a_file_on_disk(self, article):
        for record in load(article, "videos.json"):
            if record["local_file"]:
                assert (article / record["local_file"]).is_file()


class TestEveryXPathResolves:
    def test_no_block_carries_an_xpath_that_does_not_resolve(self, article):
        tree = reader_tree(article)
        blocks = load(article, "content.json")["blocks"]
        assert blocks
        for block in blocks:
            assert block["xpath"], block
            assert resolve(tree, block["xpath"]) is not None, block["xpath"]

    def test_every_block_has_a_valid_type(self, article):
        from causalia_extractor.models import BLOCK_TYPES
        for block in load(article, "content.json")["blocks"]:
            assert block["type"] in BLOCK_TYPES

    def test_every_block_has_type_index_and_xpath(self, article):
        for block in load(article, "content.json")["blocks"]:
            assert {"type", "index", "xpath"} <= set(block)


class TestNoBlockIdAnywhere:
    def test_no_artifact_mentions_block_id(self, article):
        for name in ("content.json", "images.json", "videos.json", "links.json",
                     "article.json"):
            assert "block_id" not in (article / name).read_text(encoding="utf-8")


class TestIdempotency:
    def test_two_runs_produce_equivalent_artifacts(self, tmp_path):
        from causalia_extractor.pipeline import extract
        wacz = make_wacz(tmp_path / "p" / "page.wacz", records=[
            {"uri": ARTICLE_URL, "content_type": "text/html",
             "body": html_document(RICH_BODY)},
            {"uri": "https://cdn.example.com/kep.jpg",
             "content_type": "image/jpeg", "body": JPEG_IMAGE}])
        first = extract(wacz, tmp_path / "one").output_dir
        second = extract(wacz, tmp_path / "two").output_dir

        names = sorted(p.name for p in first.rglob("*") if p.is_file())
        assert names == sorted(p.name for p in second.rglob("*") if p.is_file())
        for name in names:
            left = next(first.rglob(name)).read_bytes()
            right = next(second.rglob(name)).read_bytes()
            if name == "extraction.json":
                # extracted_at is a lifecycle timestamp, not part of any id.
                a, b = json.loads(left), json.loads(right)
                a.pop("extracted_at"), b.pop("extracted_at")
                assert a == b
            else:
                assert left == right, name

    def test_ids_carry_no_timestamp_or_randomness(self, tmp_path):
        from causalia_extractor.pipeline import extract
        wacz = make_wacz(tmp_path / "p" / "page.wacz", records=[
            {"uri": ARTICLE_URL, "content_type": "text/html",
             "body": html_document(RICH_BODY)},
            {"uri": "https://cdn.example.com/kep.jpg",
             "content_type": "image/jpeg", "body": JPEG_IMAGE}])
        directory = extract(wacz, tmp_path / "out").output_dir
        for record in load(directory, "images.json"):
            assert record["id"].startswith("image_")
            assert record["id"][6:].isdigit()
