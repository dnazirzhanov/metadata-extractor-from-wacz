"""XPath generation against the canonical document, and its validation."""

import lxml.html as LH
import pytest

from causalia_extractor.xpath import (
    XPathMismatch, resolve, validate, xpath_for, xpath_for_validated)

DOCUMENT = """<html><body><article>
<header><h1>Cim</h1></header>
<div class="article-body">
<figure><img src="a.jpg"><figcaption>alairas</figcaption></figure>
<p>egy</p><p>ketto</p><h2>alcim</h2><p>harom</p>
<figure><video src="v.mp4"></video></figure>
<blockquote>idezet</blockquote>
<ul><li>a</li><li>b</li></ul>
<div><div><p>melyen</p></div></div>
</div></article></body></html>"""


@pytest.fixture
def tree():
    return LH.document_fromstring(DOCUMENT).getroottree()


class TestGeneratedForm:
    def test_siblings_of_the_same_tag_are_indexed(self, tree):
        paragraphs = tree.xpath("/html/body/article/div/p")
        assert [xpath_for(p) for p in paragraphs] == [
            "/html/body/article/div/p[1]",
            "/html/body/article/div/p[2]",
            "/html/body/article/div/p[3]",
        ]

    def test_an_only_child_of_its_tag_has_no_predicate(self, tree):
        heading = tree.xpath("//h2")[0]
        assert xpath_for(heading) == "/html/body/article/div/h2"

    def test_a_figure_indexes_but_its_only_image_does_not(self, tree):
        image = tree.xpath("//img")[0]
        assert xpath_for(image) == "/html/body/article/div/figure[1]/img"

    def test_the_second_figure_wraps_the_video(self, tree):
        video = tree.xpath("//video")[0]
        assert xpath_for(video) == "/html/body/article/div/figure[2]/video"

    def test_list_items_are_indexed(self, tree):
        items = tree.xpath("//li")
        assert [xpath_for(i) for i in items] == [
            "/html/body/article/div/ul/li[1]",
            "/html/body/article/div/ul/li[2]",
        ]

    def test_nested_structures_are_pathed_in_full(self, tree):
        deep = tree.xpath("//p[text()='melyen']")[0]
        assert xpath_for(deep) == "/html/body/article/div/div/div/p"

    def test_every_element_in_the_document_round_trips(self, tree):
        # The generator is not hardcoded to a shape; it must describe whatever
        # element it is handed.
        for element in tree.getroot().iter():
            if isinstance(element.tag, str):
                assert resolve(tree, xpath_for(element)) is element


class TestValidation:
    def test_a_correct_path_validates(self, tree):
        first = tree.xpath("/html/body/article/div/p[1]")[0]
        validate(tree, first, "/html/body/article/div/p[1]")

    def test_a_path_to_a_different_element_is_rejected(self, tree):
        first = tree.xpath("/html/body/article/div/p[1]")[0]
        with pytest.raises(XPathMismatch):
            validate(tree, first, "/html/body/article/div/p[2]")

    def test_a_path_that_selects_nothing_is_rejected(self, tree):
        first = tree.xpath("/html/body/article/div/p[1]")[0]
        with pytest.raises(XPathMismatch):
            validate(tree, first, "/html/body/article/div/p[99]")

    def test_an_ambiguous_path_is_rejected(self, tree):
        # Selecting three paragraphs is a failure, not something to resolve by
        # taking the first hit.
        assert resolve(tree, "//p") is None

    def test_a_malformed_expression_returns_none_rather_than_raising(self, tree):
        assert resolve(tree, "/html/[[") is None

    def test_generate_and_validate_is_the_single_entry_point(self, tree):
        element = tree.xpath("//blockquote")[0]
        assert xpath_for_validated(tree, element) == \
            "/html/body/article/div/blockquote"
