# Ported from causalia-final/extractor/sites/ripost.py
"""
extractor/sites/ripost.py
=========================
Rules specific to ripost.hu.

Everything here was learned by looking at real captures from this corpus.
Each rule exists because the generic path got it visibly wrong.
"""

from __future__ import annotations

import base64
import re

from .base import SiteRules

#: ripost.hu serves images through an imgproxy-style CDN whose path is
#:   /<yyyy>/<mm>/<signature>/<resize-op>/<w>/<h>/no/1/<base64-origin>.webp
#: The trailing segment is the base64url of the ORIGIN url, so two
#: renditions of one photograph share it while differing in everything
#: else. Matching on it is what lets us tell "the same picture at another
#: size" apart from "a different picture".
_CDN_ORIGIN_RE = re.compile(r"/([A-Za-z0-9_-]{40,})\.(?:webp|jpg|jpeg|png)$")


class RipostRules(SiteRules):
    outlet = "ripost.hu"

    publisher = "Ripost"
    site_name = "Ripost"
    default_language = "hu"

    #: ripost.hu serves the SAME generic ``og_image.png`` on every single
    #: article. Taking og:image at face value would file the site logo as
    #: the lead photo of all 285,606 pieces. ``/mw/`` is its share-widget
    #: image path.
    generic_image_markers = (
        "/assets/images/og_image",
        "/assets/images/logo",
        "placeholder",
        "default-share",
        "/mw/",
        "sprite",
        "1x1",
    )

    subtitle_selector = ".lead, .article-lead, .cikk-lead, h2.lead"
    author_selector = ".author-name, .author, [rel=author], [itemprop=author]"

    def accept_author(self, name: str) -> bool:
        """Reject the publisher's own name as a byline.

        ripost.hu's JSON-LD ``author`` is almost always an Organization
        named "Ripost", not a person. Recording that as the author makes
        every article look like it has a known byline when in fact it has
        none - which would be actively misleading in an evidence system
        that later wants to reason about who claimed what.

        The organisation is still preserved: it lands in ``publisher`` and
        ``site_name``, which is where it belongs.
        """
        if not super().accept_author(name):
            return False
        return name.strip().casefold() not in ("ripost", "ripost.hu", "ripost szerkesztőség")

    def image_identity(self, url: str) -> str:
        """Identify the source photograph behind a ripost CDN URL.

        Measured on a 50-article sample: 23 articles declared a JSON-LD
        lead image that was not in the capture, and in 2 of those the
        declared image was simply another rendition of a photo already in
        the article body. Without this, those 2 produced a spurious
        "lead image not captured" note and an avoidable duplicate check.
        """
        match = _CDN_ORIGIN_RE.search(url or "")
        if not match:
            return super().image_identity(url)
        encoded = match.group(1)
        encoded += "=" * (-len(encoded) % 4)
        try:
            return base64.urlsafe_b64decode(encoded).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return super().image_identity(url)


#: The registry imports this.
RULES = RipostRules()
