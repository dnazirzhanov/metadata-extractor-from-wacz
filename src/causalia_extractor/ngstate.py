"""
ngstate.py
==========
Recover the article body from an Angular app's own TransferState blob.

Ported from ``causalia-final/extractor/core/ngstate.py``. Necessary, not
optional: magyarnemzet.hu alone is ~1.1M articles, and before this fallback
existed 59 of 60 sampled magyarnemzet articles were silently truncated - mean
blocks 3.2, median 3. With it, mean 13.3 and median 10.

Used ONLY as a fallback, and only when it wins by a real margin, so that a
well-served page is never second-guessed.
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

from bs4 import BeautifulSoup


def _iter_article_nodes(node):
    """Every dict that looks like an article record with a body."""
    if isinstance(node, dict):
        if isinstance(node.get("body"), list) and node["body"]:
            yield node
        for value in node.values():
            yield from _iter_article_nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_article_nodes(value)


def _wysiwyg_html(body) -> str:
    """Concatenate the rich-text fragments of one article's body, in order."""
    chunks: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("key") == "text" and isinstance(node.get("value"), str):
                chunks.append(node["value"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(body)
    return "".join(chunks)


def _url_slug(url: str) -> str:
    segments = [s for s in urlparse(url or "").path.split("/") if s]
    return segments[-1].lower() if segments else ""


def article_body_html(html: str, url: str) -> str | None:
    """The current article's body HTML from ng-state, or None.

    Returns None for every ambiguity - a missing blob, unparseable JSON,
    no article node, or several candidates none of which match the URL.
    The caller treats this as "no better source available" and keeps what
    Readability produced.
    """
    soup = BeautifulSoup(html or "", "lxml")
    tag = soup.find("script", attrs={"id": "ng-state"})
    if tag is None:
        return None
    try:
        data = json.loads(tag.string or tag.get_text() or "")
    except (ValueError, TypeError):
        return None

    nodes = list(_iter_article_nodes(data))
    if not nodes:
        return None

    slug = _url_slug(url)
    matched = [n for n in nodes
               if isinstance(n.get("slug"), str) and n["slug"].lower() == slug]
    if not matched and len(nodes) == 1:
        matched = nodes
    if not matched:
        return None

    body_html = _wysiwyg_html(matched[0]["body"])
    return body_html or None


def text_length(html: str) -> int:
    """Visible-character count, for deciding whether a body is too thin."""
    if not html:
        return 0
    return len(" ".join(BeautifulSoup(html, "lxml").get_text(" ", strip=True).split()))
