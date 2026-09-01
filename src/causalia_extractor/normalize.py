r"""
normalize.py
============
THE canonical text normalisation for this system.

Every character offset Causalia stores is an offset into the string this module
produces. Block text, selector offsets, quote text and selector verification all
go through ``normalize_text``. There is deliberately only one definition.

WHY IT IS SHAPED THIS WAY
-------------------------
The first-generation extractor built block text with BeautifulSoup's
``get_text(" ", strip=True)``, which injects a separator at every inline
boundary. Measured on the live corpus (ripost.hu ``00003bdc...``), the captured
DOM holds::

    tobb <strong>Spike Lee</strong>-filmben is szerepelt

and ``content.json`` recorded::

    tobb Spike Lee -filmben is szerepelt

The same defect produced ``mondta Wyckoff .`` and ``Byrd -t``. In Hungarian
those hyphenated suffixes are part of the word, so the stored text was not
merely cosmetically different from the page - it was a different string, and any
character offset computed against it pointed somewhere else in the real
document.

So: text nodes are concatenated with NO separator, exactly as a browser's
``textContent`` does, and whitespace is collapsed afterwards.

AGREEMENT WITH THE BROWSER
--------------------------
A frontend must be able to recompute this from the live DOM, or the offsets we
store are unverifiable. The JavaScript equivalent of ``normalize_text`` is::

    el.textContent.replace(/[\u0009\u000a\u000b\u000c\u000d\u0020\u001c\u001d\u001e\u001f\u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+/gu, ' ').trim()

``WHITESPACE_CLASS`` below is that same class. It is spelled out explicitly
rather than using ``\s`` because Python's ``\s`` and JavaScript's ``\s`` do not
agree: Python matches U+001C-U+001F and U+0085 which JavaScript does not, and
JavaScript matches U+FEFF which Python does not. The union of both is used, so
the two languages produce identical output. U+0085 is not hypothetical here - it
occurs in real article titles in this corpus and has already broken a JSON Lines
reader once.

NO UNICODE NORMALISATION
------------------------
``normalize_text`` deliberately does NOT apply NFC or NFKC. NFKC changes string
lengths relative to the live DOM (ligatures, full-width forms, some Hungarian
compatibility characters), which would silently shift every stored offset away
from what the browser measures. Comparing text for identity is a separate
concern from addressing it; this function is for addressing.
"""

from __future__ import annotations

import re

# The union of Python's and JavaScript's whitespace, so that both languages
# collapse exactly the same characters. See the module docstring.
#: Codepoints, not literals: an invisible character in source is a bug
#: waiting to happen, and this list is the contract with the frontend.
WHITESPACE_CODEPOINTS = (
    0x0009, 0x000A, 0x000B, 0x000C, 0x000D, 0x0020, 0x001C, 0x001D,
    0x001E, 0x001F, 0x0085, 0x00A0, 0x1680, 0x2000, 0x2001, 0x2002,
    0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200A,
    0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF,
)
WHITESPACE_CHARS = "".join(chr(c) for c in WHITESPACE_CODEPOINTS)

WHITESPACE_CLASS = "[" + re.escape(WHITESPACE_CHARS) + "]+"
_WHITESPACE_RE = re.compile(WHITESPACE_CLASS)

# Elements that force a line break inside otherwise inline content. Without
# this, "foo<br>bar" would normalise to "foobar" and join two words that a
# reader sees on separate lines.
_BREAK_TAGS = frozenset({"br"})


def collapse(text: str) -> str:
    """Collapse whitespace runs to one space and strip both ends.

    This is the second half of ``normalize_text``, exposed separately so that
    strings which did not come from a DOM element (a caption attribute, an alt
    text, a title) can be normalised the same way.
    """
    if not text:
        return ""
    return _WHITESPACE_RE.sub(" ", text).strip()


def element_raw_text(element) -> str:
    """Concatenate descendant text in document order, as ``textContent`` does.

    Accepts an lxml element. ``<br>`` becomes a newline so that the collapse
    step turns it into a single space rather than joining the words either side.
    No separator is inserted at any other boundary - that is the whole point.
    """
    parts: list[str] = []

    def walk(node) -> None:
        for child in node:
            tag = child.tag
            if not isinstance(tag, str):
                # A comment or processing instruction: lxml gives it a callable
                # tag, and its .text is the comment body. textContent does not
                # include that, so only the tail is taken below.
                pass
            elif tag.lower() in _BREAK_TAGS:
                parts.append("\n")
            else:
                if child.text:
                    parts.append(child.text)
                walk(child)
            if child.tail:
                parts.append(child.tail)

    if element.text:
        parts.append(element.text)
    walk(element)
    return "".join(parts)


def normalize_text(element) -> str:
    """The logical visible text of an lxml element.

    ``<p>Donald <strong>Trump</strong> announced\\n<em>something</em>.</p>``
    becomes ``Donald Trump announced something.``
    """
    return collapse(element_raw_text(element))
