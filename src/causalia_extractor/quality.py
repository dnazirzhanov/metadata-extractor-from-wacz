"""
quality.py
==========
One verdict per extraction, computed from the artifacts that were just written.

    success        every required field is present and nothing warned
    partial_valid  the ARTICLE is usable; something optional is missing
    invalid        the article cannot safely be used and must never be ingested

WHY THIS EXISTS AS A MODULE
---------------------------
Until now the pipeline computed ``partial if (warnings or not article_blocks)``
inline, which put two very different facts under one word: "this article has no
author" and "this article has no body". The second one matters - an article with
no content block has no citable passage, which is the thing this corpus exists
to serve, and it was being ingested and answering searches on its metadata
alone. Measured on mandiner: 1.28% of the outlet, ~5,400 articles.

The rules are deliberately derived from what the extractor actually produces and
what the schema actually requires, not from a general notion of completeness. A
gate that rejects two thirds of an outlet is a design error, not a standard:

  * author is missing on 66.8% of mandiner, and a check of 80 author-less
    captures found an article-level author in NONE of them. It cannot be
    required.
  * subtitle is absent on 100% of mandiner because the outlet puts the
    standfirst in ``description`` (99.8% present, same search weight).
  * publication date is missing on 1.9%. It is a filter, not an identity.

THE MAPPING TO THE DATABASE
---------------------------
``corpus.article_extraction.extraction_status`` is CHECK-constrained to
('success', 'partial', 'failed') and is not being widened - the vocabulary here
maps onto it. ``invalid`` maps to ``failed``, which is what finally makes that
value reachable: before this, every failure path returned no output directory at
all, so ``failed`` could not be written by any producer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import TEXTUAL_TYPES

QUALITY_SUCCESS = "success"
QUALITY_PARTIAL_VALID = "partial_valid"
QUALITY_INVALID = "invalid"

USABLE = (QUALITY_SUCCESS, QUALITY_PARTIAL_VALID)

#: extraction_status to write for each verdict. The corpus schema's CHECK.
STATUS_FOR_QUALITY = {
    QUALITY_SUCCESS: "success",
    QUALITY_PARTIAL_VALID: "partial",
    QUALITY_INVALID: "failed",
}

# -- reason codes ------------------------------------------------------
# Stable strings, not prose: they are counted in SQL and grouped in a
# dashboard, so they must not drift with a reworded message.

NO_CONTENT_BLOCKS = "no_content_blocks"
NO_PAGE_URL = "no_page_url"
NO_HTML_DOCUMENT = "no_html_document"
ARCHIVE_UNREADABLE = "archive_unreadable"
EXTRACTION_ERROR = "extraction_error"
ARCHIVE_NOT_STATABLE = "archive_not_statable"

NO_TITLE = "no_title"
NO_PUBLISHED_AT = "no_published_at"
EXTRACTION_WARNINGS = "extraction_warnings"

#: Every reason that means "never ingest this".
INVALID_REASONS = frozenset({
    NO_CONTENT_BLOCKS, NO_PAGE_URL, NO_HTML_DOCUMENT, ARCHIVE_UNREADABLE,
    EXTRACTION_ERROR, ARCHIVE_NOT_STATABLE,
})


@dataclass(frozen=True)
class Verdict:
    quality: str
    reasons: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """True when this article may be ingested and become searchable."""
        return self.quality in USABLE

    @property
    def extraction_status(self) -> str:
        return STATUS_FOR_QUALITY[self.quality]


def invalid(reason: str) -> Verdict:
    """A verdict for a failure that happened before blocks could be built."""
    return Verdict(QUALITY_INVALID, [reason])


def prose_blocks(blocks) -> list:
    """Blocks that carry citable text.

    Image and video blocks are real blocks with a real XPath, but they hold no
    text, so an article consisting only of them has nothing to cite and nothing
    to search. Counting them as content is what let an empty article look
    populated.
    """
    return [b for b in blocks
            if b.type in TEXTUAL_TYPES and (b.text or "").strip()]


def assess(blocks, *, title, published_at, warnings) -> Verdict:
    """The verdict for one completed extraction.

    ``warnings`` is the pipeline's own list. Every warning makes an article
    partial_valid rather than success - that is the pre-existing behaviour and
    it is kept. What changes is that an article with no prose block is now
    INVALID rather than partial.
    """
    if not prose_blocks(blocks):
        return invalid(NO_CONTENT_BLOCKS)

    reasons: list[str] = []
    if not title:
        reasons.append(NO_TITLE)
    if not published_at:
        reasons.append(NO_PUBLISHED_AT)
    if warnings:
        reasons.append(EXTRACTION_WARNINGS)

    return Verdict(QUALITY_PARTIAL_VALID if reasons else QUALITY_SUCCESS, reasons)
