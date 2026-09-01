"""
selectors.py
============
The canonical way Causalia points at an exact passage inside the canonical
document.

    XPath                  which element
    TextPositionSelector   which character range inside it, [start, end)
    quote.exact            what that range must say

Shape::

    {
      "type": "XPathSelector",
      "value": "/html/body/article/div/p[3]",
      "refinedBy": {"type": "TextPositionSelector", "start": 10, "end": 38},
      "quote": {"exact": "the exact referenced text",
                "prefix": "...up to 32 chars before...",
                "suffix": "...up to 32 chars after..."}
    }

OFFSET SEMANTICS
----------------
``start`` is inclusive, ``end`` is exclusive, so ``text = "Hello world"`` with
``start=0, end=5`` selects ``"Hello"``. Offsets are character offsets into
``normalize_text(element)`` - the logical visible text - and never into the HTML
source. See ``normalize.py``.

WHY THE QUOTE IS STORED TOO
---------------------------
It is deliberate redundancy. Resolving a selector walks XPath -> element ->
normalised text -> ``[start:end]`` and then compares the result with
``quote.exact``. If they differ the selector is INVALID and must be reported as
such. Highlighting a different passage instead would be a fabricated citation,
which is the one failure this system cannot tolerate.

This matters because positional XPath drifts. Measured on this corpus
(2026-08-25, 402 passages): after a single paragraph was inserted at the top of
the article by an ordinary extractor fix, only 43% of positional selectors still
resolved to their intended element - the other 57% resolved to the WRONG element
and would have silently mis-highlighted. The quote check converts every one of
those into a loud, detectable failure.

``prefix`` and ``suffix`` are the repair path: 32 characters of surrounding text
that let a resolver re-find the passage by content when the position has moved.
They are never consulted while the XPath resolves and the quote matches.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .normalize import normalize_text
from .xpath import resolve, xpath_for_validated

#: Characters of context stored either side of the quote for repair.
CONTEXT_CHARS = 32

# Verification outcomes.
OK = "ok"
XPATH_UNRESOLVED = "xpath_unresolved"
RANGE_OUT_OF_BOUNDS = "range_out_of_bounds"
QUOTE_MISMATCH = "quote_mismatch"


class SelectorError(Exception):
    """A selector could not be built or did not verify against its own document."""


@dataclass
class Selector:
    """An XPathSelector refined by a character range, with its quote."""

    value: str
    start: int
    end: int
    exact: str
    prefix: str = ""
    suffix: str = ""

    def to_dict(self) -> dict:
        quote: dict = {"exact": self.exact}
        if self.prefix:
            quote["prefix"] = self.prefix
        if self.suffix:
            quote["suffix"] = self.suffix
        return {
            "type": "XPathSelector",
            "value": self.value,
            "refinedBy": {
                "type": "TextPositionSelector",
                "start": self.start,
                "end": self.end,
            },
            "quote": quote,
        }


def make_selector(tree, element, start: int, end: int) -> Selector:
    """Build a verified selector for ``[start, end)`` of ``element``'s text.

    Raises ``SelectorError`` if the range is empty, reversed, or outside the
    element's normalised text.
    """
    if start < 0 or end < start:
        raise SelectorError(f"invalid range [{start}, {end})")
    text = normalize_text(element)
    if end > len(text):
        raise SelectorError(
            f"range [{start}, {end}) exceeds the element's {len(text)} characters")
    if start == end:
        raise SelectorError("an empty range selects no passage")

    selector = Selector(
        value=xpath_for_validated(tree, element),
        start=start,
        end=end,
        exact=text[start:end],
        prefix=text[max(0, start - CONTEXT_CHARS):start],
        suffix=text[end:end + CONTEXT_CHARS],
    )
    status = verify(tree, selector)
    if status != OK:
        # Only reachable through a bug in this module; a selector that cannot
        # verify against the very document it was built from must never ship.
        raise SelectorError(f"selector failed self-verification: {status}")
    return selector


def find_selector(tree, element, needle: str, *, occurrence: int = 1):
    """Build a selector for the ``occurrence``-th appearance of ``needle``.

    Returns None when the text is not present, which is a normal outcome - an
    anchor's own text can differ from what survives normalisation.
    """
    text = normalize_text(element)
    needle = " ".join(needle.split()) if needle else ""
    if not needle:
        return None
    position = -1
    for _ in range(occurrence):
        position = text.find(needle, position + 1)
        if position < 0:
            return None
    return make_selector(tree, element, position, position + len(needle))


def verify(tree, selector: Selector) -> str:
    """Resolve a selector end to end and report what happened.

    Returns ``OK`` or one of the failure constants. This is the function a
    consumer of the artifacts should mirror.
    """
    element = resolve(tree, selector.value)
    if element is None:
        return XPATH_UNRESOLVED
    text = normalize_text(element)
    if selector.end > len(text) or selector.start > selector.end:
        return RANGE_OUT_OF_BOUNDS
    if text[selector.start:selector.end] != selector.exact:
        return QUOTE_MISMATCH
    return OK


def verify_payload(tree, payload: dict) -> str:
    """``verify`` for a selector already serialised to its dict form."""
    try:
        refined = payload["refinedBy"]
        selector = Selector(
            value=payload["value"],
            start=int(refined["start"]),
            end=int(refined["end"]),
            exact=payload["quote"]["exact"],
        )
    except (KeyError, TypeError, ValueError):
        return QUOTE_MISMATCH
    return verify(tree, selector)
