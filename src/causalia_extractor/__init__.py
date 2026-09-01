"""Causalia article extractor, second generation.

Turns one Browsertrix ``page.wacz`` into an article directory whose content
references are anchored to a canonical document:

    readability.html  is the canonical document
    XPath             identifies the element
    TextPositionSelector  identifies the exact character range
    quote.exact       verifies the evidence

This package is standalone by construction. It never imports the archiver, it
opens no socket and no database connection, and it treats every ``.wacz`` as
strictly read-only (checked by a stat fence after every extraction).
"""

__version__ = "2.0.0"
EXTRACTOR_NAME = "causalia-article-extractor"
