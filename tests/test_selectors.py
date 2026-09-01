"""Exact passage selectors: offsets, quote verification, and mismatch detection."""

import lxml.html as LH
import pytest

from causalia_extractor import selectors as sel
from causalia_extractor.selectors import (
    CONTEXT_CHARS, Selector, SelectorError, find_selector, make_selector, verify)

DOCUMENT = """<html><body><article><div class="article-body">
<p>Hello world</p>
<p>Donald <strong>Trump</strong> announced <em>something</em> today.</p>
<p>Egy szo, majd megint egy szo, es megint egy szo.</p>
</div></article></body></html>"""


@pytest.fixture
def tree():
    return LH.document_fromstring(DOCUMENT).getroottree()


@pytest.fixture
def first(tree):
    return tree.xpath("/html/body/article/div/p[1]")[0]


@pytest.fixture
def inline(tree):
    return tree.xpath("/html/body/article/div/p[2]")[0]


class TestOffsetSemantics:
    def test_start_is_inclusive_and_end_is_exclusive(self, tree, first):
        selector = make_selector(tree, first, 0, 5)
        assert selector.exact == "Hello"

    def test_the_whole_text_is_selectable(self, tree, first):
        selector = make_selector(tree, first, 0, len("Hello world"))
        assert selector.exact == "Hello world"

    def test_an_empty_range_is_refused(self, tree, first):
        with pytest.raises(SelectorError):
            make_selector(tree, first, 3, 3)

    def test_a_reversed_range_is_refused(self, tree, first):
        with pytest.raises(SelectorError):
            make_selector(tree, first, 5, 2)

    def test_a_range_past_the_end_is_refused(self, tree, first):
        with pytest.raises(SelectorError):
            make_selector(tree, first, 0, 999)


class TestOffsetsAreOverVisibleText:
    def test_offsets_ignore_the_html_tags(self, tree, inline):
        # "Donald Trump announced something today." - offset 7 is 'T' of Trump,
        # which sits inside <strong>. An offset over the HTML source would land
        # in the middle of the tag instead.
        selector = make_selector(tree, inline, 7, 12)
        assert selector.exact == "Trump"

    def test_a_range_spanning_an_inline_boundary_works(self, tree, inline):
        selector = make_selector(tree, inline, 7, 22)
        assert selector.exact == "Trump announced"


class TestQuoteVerification:
    def test_a_freshly_built_selector_verifies(self, tree, first):
        assert verify(tree, make_selector(tree, first, 0, 5)) == sel.OK

    def test_a_shifted_range_is_reported_as_a_quote_mismatch(self, tree, first):
        selector = make_selector(tree, first, 0, 5)
        selector.start, selector.end = 6, 11        # now selects "world"
        assert verify(tree, selector) == sel.QUOTE_MISMATCH

    def test_a_mismatch_is_never_silently_accepted(self, tree, first):
        # The failure this check exists to prevent: highlighting a passage the
        # citation did not refer to.
        selector = make_selector(tree, first, 0, 5)
        selector.exact = "Goodbye"
        assert verify(tree, selector) != sel.OK

    def test_an_unresolvable_xpath_is_reported_as_such(self, tree, first):
        selector = make_selector(tree, first, 0, 5)
        selector.value = "/html/body/article/div/p[99]"
        assert verify(tree, selector) == sel.XPATH_UNRESOLVED

    def test_a_range_out_of_bounds_is_reported_before_the_quote_is_compared(
            self, tree, first):
        selector = make_selector(tree, first, 0, 5)
        selector.end = 500
        assert verify(tree, selector) == sel.RANGE_OUT_OF_BOUNDS

    def test_an_xpath_pointing_at_a_different_paragraph_is_caught(self, tree, first):
        # This is the measured drift case: after one paragraph is inserted, a
        # positional xpath resolves to the WRONG element. The quote turns that
        # from a silent mis-highlight into a detected failure.
        selector = make_selector(tree, first, 0, 5)
        selector.value = "/html/body/article/div/p[2]"
        assert verify(tree, selector) == sel.QUOTE_MISMATCH


class TestSerialisedShape:
    def test_the_payload_is_the_canonical_format(self, tree, first):
        payload = make_selector(tree, first, 0, 5).to_dict()
        assert payload["type"] == "XPathSelector"
        assert payload["value"] == "/html/body/article/div/p[1]"
        assert payload["refinedBy"] == {
            "type": "TextPositionSelector", "start": 0, "end": 5}
        assert payload["quote"]["exact"] == "Hello"

    def test_a_serialised_selector_verifies_through_verify_payload(self, tree, first):
        payload = make_selector(tree, first, 0, 5).to_dict()
        assert sel.verify_payload(tree, payload) == sel.OK

    def test_a_tampered_payload_is_rejected(self, tree, first):
        payload = make_selector(tree, first, 0, 5).to_dict()
        payload["refinedBy"]["end"] = 11
        assert sel.verify_payload(tree, payload) == sel.QUOTE_MISMATCH

    def test_a_malformed_payload_does_not_raise(self, tree):
        assert sel.verify_payload(tree, {"nonsense": True}) == sel.QUOTE_MISMATCH


class TestRepairContext:
    def test_context_is_captured_either_side(self, tree, inline):
        selector = make_selector(tree, inline, 7, 12)
        assert selector.prefix == "Donald "
        assert selector.suffix.startswith(" announced")

    def test_context_is_bounded(self, tree, inline):
        selector = make_selector(tree, inline, 20, 25)
        assert len(selector.prefix) <= CONTEXT_CHARS
        assert len(selector.suffix) <= CONTEXT_CHARS

    def test_context_is_omitted_from_the_payload_when_empty(self, tree, first):
        payload = make_selector(tree, first, 0, 5).to_dict()
        assert "prefix" not in payload["quote"]
        assert payload["quote"]["suffix"] == " world"


class TestFindSelector:
    def test_it_locates_a_passage_by_its_text(self, tree, inline):
        selector = find_selector(tree, inline, "announced")
        assert selector.exact == "announced"
        assert verify(tree, selector) == sel.OK

    def test_it_can_select_a_later_occurrence(self, tree):
        paragraph = tree.xpath("/html/body/article/div/p[3]")[0]
        first = find_selector(tree, paragraph, "egy szo", occurrence=1)
        second = find_selector(tree, paragraph, "egy szo", occurrence=2)
        assert first.start < second.start
        assert verify(tree, first) == sel.OK
        assert verify(tree, second) == sel.OK

    def test_asking_past_the_last_occurrence_yields_nothing(self, tree):
        paragraph = tree.xpath("/html/body/article/div/p[3]")[0]
        assert find_selector(tree, paragraph, "egy szo", occurrence=9) is None

    def test_absent_text_yields_no_selector_rather_than_a_wrong_one(self, tree, first):
        assert find_selector(tree, first, "not in this paragraph") is None

    def test_empty_text_yields_no_selector(self, tree, first):
        assert find_selector(tree, first, "   ") is None
