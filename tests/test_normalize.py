"""The canonical text normalisation - the definition every offset depends on."""

import lxml.html as LH
import pytest

from causalia_extractor.normalize import (
    WHITESPACE_CHARS, collapse, normalize_text)

NBSP = chr(0x00A0)
NEL = chr(0x0085)
LINE_SEP = chr(0x2028)
BOM = chr(0xFEFF)
ZWSP = chr(0x200B)


def text_of(html: str) -> str:
    return normalize_text(LH.fragment_fromstring(html))


class TestInlineMarkupIsNotASeparator:
    """The defect this function exists to prevent, measured on the live corpus."""

    def test_inline_elements_do_not_insert_a_space(self):
        assert text_of(
            "<p>Donald <strong>Trump</strong> announced\n"
            "   <em>something</em>.</p>") == "Donald Trump announced something."

    def test_a_hungarian_suffix_stays_attached_to_its_proper_noun(self):
        # Measured on ripost.hu 00003bdc...: the first-generation extractor
        # stored "Spike Lee -filmben", which is a different word.
        assert text_of("<p>tobb <strong>Spike Lee</strong>-filmben</p>") == \
            "tobb Spike Lee-filmben"

    def test_punctuation_after_an_inline_element_is_not_pushed_away(self):
        assert text_of("<p>mondta <strong>Wyckoff</strong>.</p>") == "mondta Wyckoff."

    def test_a_colon_inside_a_link_stays_attached(self):
        assert text_of('<p>Facebook<a href="#">:</a> igen</p>') == "Facebook: igen"

    def test_nested_inline_elements_concatenate_with_nothing_between(self):
        assert text_of("<p>a<span>b</span>c</p>") == "abc"


class TestWhitespace:
    def test_runs_collapse_to_one_space(self):
        assert text_of("<p>  lead   and\ttrail  </p>") == "lead and trail"

    def test_a_line_break_element_separates_words(self):
        # Without this "foo<br>bar" would normalise to "foobar" and join two
        # words a reader sees on separate lines.
        assert text_of("<p>foo<br>bar</p>") == "foo bar"

    @pytest.mark.parametrize("char", [NBSP, NEL, LINE_SEP, BOM])
    def test_exotic_whitespace_collapses(self, char):
        assert text_of(f"<p>x{char}y</p>") == "x y"

    def test_zero_width_space_is_not_whitespace(self):
        # Browsers do not treat U+200B as whitespace, so neither do we -
        # diverging would shift every offset relative to the live DOM.
        assert text_of(f"<p>zero{ZWSP}width</p>") == f"zero{ZWSP}width"

    def test_the_class_is_the_union_of_python_and_javascript_whitespace(self):
        # Python's \s matches U+001C-001F and U+0085 that JavaScript's does not;
        # JavaScript's matches U+FEFF that Python's does not. Both must be in,
        # or the frontend and the extractor disagree about an offset.
        for char in "\x1c\x1d\x1e\x1f" + NEL + BOM:
            assert char in WHITESPACE_CHARS

    def test_collapse_handles_a_bare_string(self):
        assert collapse("  a \n b  ") == "a b"
        assert collapse("") == ""
        assert collapse(None) == ""


class TestNoUnicodeNormalisation:
    def test_composed_and_decomposed_forms_are_left_as_written(self):
        # NFKC would change string lengths relative to the browser's DOM and
        # silently move every stored offset.
        composed = "á"          # a-acute, one codepoint
        decomposed = "á"       # a + combining acute, two codepoints
        assert text_of(f"<p>{composed}</p>") == composed
        assert text_of(f"<p>{decomposed}</p>") == decomposed

    def test_a_ligature_is_not_expanded(self):
        assert text_of("<p>ﬁn</p>") == "ﬁn"


class TestComments:
    def test_a_comment_body_is_not_text(self):
        assert text_of("<p>a<!-- hidden -->b</p>") == "ab"
