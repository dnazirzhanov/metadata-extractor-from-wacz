"""The canonical document and the content blocks derived from it."""

import pytest
from bs4 import BeautifulSoup

from causalia_extractor import dom
from causalia_extractor.blocks import BlockAlignmentError, build_blocks
from causalia_extractor.metadata import ArticleMetadata
from causalia_extractor.normalize import normalize_text
from causalia_extractor.xpath import resolve

META = ArticleMetadata(title="Cim", subtitle="Alcim", language="hu",
                       source_url="https://ripost.hu/a/2026/01/b",
                       published_at="2026-01-01T00:00:00Z", tags=["cimke"])


def canonical(body_html: str, metadata=META):
    """Run the build -> serialise -> reparse -> address round trip."""
    soup = BeautifulSoup(f"<html><body>{body_html}</body></html>", "lxml")
    built = dom.build(soup, metadata=metadata, wacz_name="page.wacz")
    text = dom.serialize(built.tree)
    tree = dom.reparse(text)
    blocks, block_warnings = build_blocks(tree, built.specs)
    return tree, blocks, built.warnings + block_warnings, text


class TestStructure:
    def test_blocks_are_direct_children_of_one_container(self):
        tree, blocks, _, _ = canonical(
            "<div><div><p>egy</p><p>ketto</p></div></div>")
        assert [b["xpath"] if isinstance(b, dict) else b.xpath for b in blocks] == [
            "/html/body/article/div/p[1]", "/html/body/article/div/p[2]"]

    def test_publisher_nesting_is_flattened_away(self):
        # The same paragraph must get the same xpath whatever the CMS wrapped
        # it in - that is the whole point of a controlled document.
        shallow = canonical("<p>azonos</p>")[1]
        deep = canonical(
            "<section><div class='x'><span><div><p>azonos</p></div></span></div></section>")[1]
        assert shallow[0].xpath == deep[0].xpath == "/html/body/article/div/p"

    def test_the_title_lives_in_the_header_and_is_not_a_block(self):
        tree, blocks, _, _ = canonical("<p>szoveg</p>")
        assert tree.xpath("/html/body/article/header/h1")[0].text == "Cim"
        assert [b.type for b in blocks] == ["paragraph"]

    def test_the_document_declares_the_article_language(self):
        tree, _, _, _ = canonical("<p>x</p>")
        assert tree.getroot().get("lang") == "hu"

    def test_the_page_makes_no_external_request(self):
        _, _, _, text = canonical('<p>x</p><img src="images/image_001.jpg">')
        assert "<script" not in text
        assert 'name="referrer" content="no-referrer"' in text


class TestBlockTypes:
    def test_a_paragraph_keeps_its_inline_markup(self):
        tree, blocks, _, _ = canonical(
            "<p>Donald <strong>Trump</strong> mondta <em>ezt</em>.</p>")
        assert blocks[0].type == "paragraph"
        assert blocks[0].text == "Donald Trump mondta ezt."
        assert tree.xpath("//strong")            # the markup survived

    def test_inline_elements_are_not_blocks_of_their_own(self):
        _, blocks, _, _ = canonical(
            "<p>a <strong>b</strong> <em>c</em> <a href='https://x.hu'>d</a> e</p>")
        assert len(blocks) == 1

    def test_headings_carry_their_level_and_are_never_h1(self):
        _, blocks, _, _ = canonical("<h1>egy</h1><h3>harom</h3>")
        assert [(b.type, b.level) for b in blocks] == [("heading", 2), ("heading", 3)]

    def test_a_blockquote_becomes_one_quote_block(self):
        _, blocks, _, _ = canonical(
            "<blockquote><p>Elso mondat.</p><p>Masodik.</p></blockquote>")
        assert blocks[0].type == "quote"
        # The two inner paragraphs must not run together into "mondat.Masodik."
        assert blocks[0].text == "Elso mondat. Masodik."

    def test_a_list_addresses_each_item(self):
        tree, blocks, _, _ = canonical("<ul><li>elso</li><li>masodik</li></ul>")
        block = blocks[0]
        assert block.type == "list"
        assert block.xpath == "/html/body/article/div/ul"
        assert [i.text for i in block.items] == ["elso", "masodik"]
        assert [i.xpath for i in block.items] == [
            "/html/body/article/div/ul/li[1]", "/html/body/article/div/ul/li[2]"]
        for item in block.items:
            assert normalize_text(resolve(tree, item.xpath)) == item.text

    def test_list_items_do_not_run_together_in_the_block_text(self):
        _, blocks, _, _ = canonical("<ul><li>elso</li><li>masodik</li></ul>")
        assert blocks[0].text == "elso masodik"

    def test_an_image_points_at_the_img_inside_its_figure(self):
        _, blocks, _, _ = canonical(
            '<figure><img src="images/image_001.jpg" data-image-id="image_001">'
            '<figcaption>alairas</figcaption></figure>')
        assert blocks[0].type == "image"
        assert blocks[0].xpath == "/html/body/article/div/figure/img"
        assert blocks[0].image_id == "image_001"

    def test_a_native_video_points_at_the_video_element(self):
        _, blocks, _, _ = canonical(
            '<video src="videos/video_001.mp4" data-video-id="video_001"></video>')
        assert blocks[0].type == "video"
        assert blocks[0].xpath == "/html/body/article/div/figure/video"
        assert blocks[0].video_id == "video_001"

    def test_a_third_party_embed_is_a_video_block_and_never_re_embedded(self):
        _, blocks, _, text = canonical(
            '<div data-embed-url="https://www.youtube.com/embed/abc" '
            'data-embed-platform="youtube" data-video-id="video_001"></div>')
        assert blocks[0].type == "video"
        assert blocks[0].video_id == "video_001"
        assert "<iframe" not in text

    def test_media_nested_in_a_paragraph_becomes_its_own_block(self):
        _, blocks, _, _ = canonical(
            '<p>szoveg <img src="images/image_001.jpg" data-image-id="image_001"></p>')
        assert [b.type for b in blocks] == ["paragraph", "image"]
        assert blocks[0].text == "szoveg"


class TestWhatIsNotABlock:
    def test_empty_paragraphs_are_dropped(self):
        _, blocks, _, _ = canonical("<p></p><p>   </p><p>valodi</p>")
        assert [b.text for b in blocks] == ["valodi"]

    def test_an_unsupported_leaf_is_reported_not_silently_dropped(self):
        _, blocks, warnings, _ = canonical(
            "<table><tr><td>ertek</td></tr></table><p>szoveg</p>")
        assert [b.type for b in blocks] == ["paragraph"]
        assert any("table" in w for w in warnings)


class TestLinkStrips:
    """A run of links inside the body is furniture, not a paragraph.

    Measured on mandiner.hu d1563b55: the article's own tag row sits in
    div.trending-topics INSIDE section.article-page, so it survives furniture
    stripping and Readability keeps it. It was being stored as article prose,
    which puts nine tag names into full-text search and lets a citation point at
    them.
    """

    TAGS = ('<p><a href="https://mandiner.hu/cimke/salamon_ferenc">Salamon Ferenc</a>'
            '<a href="https://mandiner.hu/cimke/zrinyi_miklos">Zrinyi Miklos</a>'
            '<a href="https://mandiner.hu/cimke/nof">NOF</a>'
            '<a href="https://mandiner.hu/cimke/szigetvar">Szigetvar</a></p>')

    def test_a_tag_strip_is_not_a_paragraph(self):
        _, blocks, _, _ = canonical(self.TAGS + "<p>A valodi elso bekezdes.</p>")
        assert [b.text for b in blocks] == ["A valodi elso bekezdes."]

    def test_a_breadcrumb_row_is_not_a_paragraph(self):
        _, blocks, _, _ = canonical(
            '<p><a href="https://x.hu/">Fooldal</a> <a href="https://x.hu/belfold">Belfold</a>'
            ' <a href="https://x.hu/belfold/2026">2026</a></p><p>Szoveg itt.</p>')
        assert [b.text for b in blocks] == ["Szoveg itt."]

    def test_a_dropped_strip_takes_its_links_with_it(self):
        # The tag chips were being recorded in links.json as article links,
        # because they had an owning block. Without the block they are not prose.
        tree, blocks, _, _ = canonical(self.TAGS + "<p>A valodi elso bekezdes.</p>")
        from causalia_extractor.links import extract_links
        records, _ = extract_links(tree, blocks, "mandiner.hu")
        assert records == []

    def test_one_long_link_is_still_prose(self):
        # A paragraph that is a single link is ordinary writing, not a strip.
        _, blocks, _, _ = canonical(
            '<p><a href="https://x.hu/a">Kamala Harris ezt irta a valaszaban</a></p>')
        assert len(blocks) == 1
        assert blocks[0].type == "paragraph"

    def test_prose_around_a_link_is_kept(self):
        _, blocks, _, _ = canonical(
            '<p>Mint megirtuk, a kormany <a href="https://x.hu/a">bejelentette</a> '
            'a dontest, es <a href="https://x.hu/b">itt</a> olvashato</p>')
        assert len(blocks) == 1
        assert "Mint megirtuk" in blocks[0].text

    def test_a_linked_sentence_is_kept(self):
        # Two links, high coverage - but it ends in a full stop, so it is prose.
        _, blocks, _, _ = canonical(
            '<p><a href="https://x.hu/a">Az elso mondat</a> '
            '<a href="https://x.hu/b">es a masodik.</a></p>')
        assert len(blocks) == 1

    def test_a_heading_of_links_is_left_alone(self):
        # Headings are short by nature; the rule applies to prose containers.
        _, blocks, _, _ = canonical(
            '<h2><a href="https://x.hu/a">Egy</a> <a href="https://x.hu/b">Ketto</a></h2>')
        assert [b.type for b in blocks] == ["heading"]

    def test_dropping_a_strip_is_not_a_warning(self):
        # Furniture is expected. Warning would mark ordinary articles `partial`.
        _, _, warnings, _ = canonical(self.TAGS + "<p>A valodi elso bekezdes.</p>")
        assert warnings == []


class TestOrdering:
    def test_indexes_are_one_based_and_contiguous(self):
        _, blocks, _, _ = canonical("<p>a</p><h2>b</h2><p>c</p>")
        assert [b.index for b in blocks] == [1, 2, 3]

    def test_document_order_is_preserved(self):
        _, blocks, _, _ = canonical(
            '<p>elso</p><figure><img src="images/image_001.jpg" '
            'data-image-id="image_001"></figure><p>masodik</p>')
        assert [b.type for b in blocks] == ["paragraph", "image", "paragraph"]

    def test_two_builds_of_the_same_input_are_identical(self):
        first = canonical("<p>a</p><p>b</p>")[3]
        second = canonical("<p>a</p><p>b</p>")[3]
        assert first == second


class TestAlignment:
    def test_a_spec_count_mismatch_is_fatal_rather_than_mislabelled(self):
        # If serialisation ever restructures the document, emitting xpaths
        # would describe a file nobody wrote. Raising is the correct outcome.
        soup = BeautifulSoup("<html><body><p>a</p></body></html>", "lxml")
        built = dom.build(soup, metadata=META, wacz_name="page.wacz")
        tree = dom.reparse(dom.serialize(built.tree))
        with pytest.raises(BlockAlignmentError):
            build_blocks(tree, built.specs + built.specs)


class TestNoBlockId:
    def test_no_block_carries_a_block_id(self):
        _, blocks, _, _ = canonical("<p>a</p><h2>b</h2><ul><li>c</li></ul>")
        for block in blocks:
            assert "block_id" not in block.to_dict()
            assert not hasattr(block, "block_id")
