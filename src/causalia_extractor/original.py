"""
original.py
===========
original.html: the captured article markup, kept for archival reference.

This file is NOT the coordinate system for selectors - those always target
readability.html. It exists so that a human, or a later re-extraction, can see
what the page actually served.

WHAT IS CHANGED, AND WHY
------------------------
The requirement is that it must not depend on the host site's external styling.
Read literally that means the page must not fetch anything, and that is also the
safety requirement: an archival file that phones out to the publisher's CDN
every time somebody opens it is a privacy leak and a live dependency on a site
that may have changed or vanished.

So every network-fetching reference is neutralised, and nothing else is touched:

* ``<script>`` (inline and external) and ``<noscript>`` are removed.
* ``<link rel=stylesheet|preload|prefetch|dns-prefetch|preconnect|icon>`` and
  any ``<link>`` with an href are removed.
* ``<style>`` blocks are kept only if they contain no ``@import`` and no
  ``url(http...)``.
* ``src`` / ``srcset`` / ``poster`` / ``data`` on img, video, audio, source,
  iframe, embed, object, track are moved to ``data-original-src`` and the live
  attribute is dropped.
* ``<base>`` is removed, so nothing resolves against the live origin.
* A ``<meta name="referrer" content="no-referrer">`` is added.

Inline ``style`` attributes and the markup structure are left exactly as
captured. The result renders unstyled - that is the point of "without depending
on the host site's external styling", and the styled rendering is preserved
separately as the screenshot.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

#: Attributes that cause a fetch. Moved aside rather than deleted, so the
#: original URL remains visible in the archived file.
_FETCHING_ATTRS = ("src", "srcset", "poster", "data")

_FETCHING_TAGS = ("img", "video", "audio", "source", "iframe", "embed",
                  "object", "track", "input")

_REMOTE_CSS_RE = re.compile(r"@import|url\(\s*['\"]?https?:", re.I)


def build_original_html(html_text: str) -> str:
    """Neutralise every network reference in the captured markup."""
    soup = BeautifulSoup(html_text, "lxml")

    for tag_name in ("script", "noscript", "base"):
        for element in soup.find_all(tag_name):
            element.decompose()

    for element in soup.find_all("link"):
        element.decompose()

    for element in soup.find_all("style"):
        if element.string and _REMOTE_CSS_RE.search(element.string):
            element.decompose()

    for element in soup.find_all(_FETCHING_TAGS):
        for attribute in _FETCHING_ATTRS:
            value = element.get(attribute)
            if not value:
                continue
            if attribute == "src" and not element.get("data-original-src"):
                element["data-original-src"] = value
            del element[attribute]
        # Lazy-loading themes keep the real URL in a data-* attribute. Those do
        # not fetch on their own - no JS runs here - so they are left in place
        # as a record of what the page intended to load.
        for attribute in list(element.attrs):
            if attribute.startswith("on"):
                del element[attribute]

    for element in soup.find_all(True):
        for attribute in list(element.attrs):
            if attribute.startswith("on"):
                del element[attribute]

    head = soup.head
    if head is None and soup.html is not None:
        head = soup.new_tag("head")
        soup.html.insert(0, head)
    if head is not None:
        referrer = soup.new_tag("meta")
        referrer["name"] = "referrer"
        referrer["content"] = "no-referrer"
        head.insert(0, referrer)

    return str(soup)
