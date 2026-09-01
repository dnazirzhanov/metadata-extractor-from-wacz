"""
links.py
========
links.json: outbound and internal links, each carrying a precise selector.

A link's place in the article is expressed by its selector and nowhere else -
there is no ``position`` and no ``block_id``::

    {"url": "https://...", "text": "napirend", "context": "...",
     "internal": false,
     "selector": {"type": "XPathSelector",
                  "value": "/html/body/article/div/p[3]",
                  "refinedBy": {"type": "TextPositionSelector",
                                "start": 15, "end": 22},
                  "quote": {"exact": "napirend", ...}}}

The selector points at the anchor's OWNING BLOCK, refined to the character range
the anchor text occupies inside it. That is deliberately not a selector for the
``<a>`` element: a citation needs to say where in the prose the link sits, and an
offset into the paragraph is what a highlighter can act on.

``rel`` is not recorded. The sanitiser rewrites it to
``noopener noreferrer nofollow`` on every anchor, so storing it would only echo
our own value back.
"""

from __future__ import annotations

from .models import IMAGE, VIDEO, LinkRecord
from .normalize import normalize_text
from .selectors import find_selector
from .urls import host_of, is_http_url


def _owning_block(element, block_elements):
    """The content block an anchor sits inside, or None if it is outside one."""
    node = element
    while node is not None:
        if node in block_elements:
            return node
        node = node.getparent()
    return None


def extract_links(tree, blocks, article_host: str) -> tuple[list[LinkRecord], list[str]]:
    """Collect every anchor in article prose, with a verified selector.

    An anchor inside an image or video block is not prose and is skipped; see
    the ``continue`` below.
    """
    from .xpath import resolve

    block_by_element = {}
    for block in blocks:
        element = resolve(tree, block.xpath)
        if element is not None:
            block_by_element[element] = block

    records: list[LinkRecord] = []
    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()
    # How many times this exact anchor text has already been seen in this block,
    # so the second "here" in a paragraph selects the second occurrence.
    occurrences: dict[tuple[int, str], int] = {}

    for anchor in tree.xpath("//a[@href]"):
        href = (anchor.get("href") or "").strip()
        if not is_http_url(href):
            continue
        text = normalize_text(anchor)
        if not text:
            continue
        key = (href, text)
        if key in seen:
            continue

        owner = _owning_block(anchor, block_by_element)
        if owner is None:
            # An anchor outside every content block - the footer's "Original
            # URL", or a link inside a figcaption. Not article prose.
            continue
        block = block_by_element[owner]
        if block.type in (IMAGE, VIDEO):
            # Not prose, and on an embed not even the publisher's markup: the
            # reader DOM writes its own "<platform>: <url>" anchor into every
            # third-party player (dom._append_video), because an offline page
            # must not re-embed one. Reading it back would be the extractor
            # citing itself - and citing the *embed* URL at that, where
            # videos.json already holds the canonical one.
            continue
        seen.add(key)

        occurrence_key = (block.index, text)
        occurrences[occurrence_key] = occurrences.get(occurrence_key, 0) + 1
        selector = find_selector(tree, owner, text,
                                 occurrence=occurrences[occurrence_key])
        if selector is None:
            warnings.append(
                f"link text {text[:40]!r} was not locatable in its own block; "
                f"recorded without a selector")

        host = host_of(href)
        records.append(LinkRecord(
            url=href,
            text=text,
            context=block.text or None,
            internal=bool(article_host) and (
                host == article_host or host.endswith("." + article_host)),
            selector=selector.to_dict() if selector is not None else None,
        ))

    return records, warnings


def links_payload(records) -> list[dict]:
    return [record.to_dict() for record in records]
