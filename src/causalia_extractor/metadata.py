"""
metadata.py
===========
article.json: the cleaned article metadata.

Ported from ``causalia-final/extractor/core/metadata.py``. The candidate chains
are the valuable part and are kept as they are - each one encodes a measured
fact about these twelve Hungarian outlets. Two of them are worth restating:

* ``alternativeHeadline`` is REJECTED as a subtitle candidate when it equals the
  headline. ripost.hu sets the two equal on essentially every article (180/180
  in a 2026-08-25 sample), and because a candidate that resolves short-circuits
  the list, the real standfirst sitting in the DOM was never consulted. The fix
  must reject the CANDIDATE, not the result.
* Tags are scoped to the article's own subtree AND rejected inside nav / menu /
  card / trending containers. The same ``/cimke/`` href pattern appears 82-222
  times per page against 0-18 real tags, in the header strip, the hamburger
  menu, and other articles' tag chips on recommendation cards.

Metadata reads the ORIGINAL, unstripped document. JSON-LD, OpenGraph and the
canonical link all live in <head> and inside elements the furniture pass is
happy to delete.

CHANGED FROM THE PORT: ``site_name`` and ``field_sources`` are gone.
``site_name`` was measured to hold the article title rather than the site on
real magyarnemzet output, and neither field is something a consumer of the
article needs. Which source won a field is a debugging question, and it is
logged rather than persisted. ``archive_id``, ``outlet`` and ``captured_at`` are
added by the pipeline, which is what knows them.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from bs4 import BeautifulSoup


# =====================================================================
# JSON-LD
# =====================================================================

def json_ld_blocks(soup: BeautifulSoup) -> list[dict]:
    """Flatten every JSON-LD block, following ``@graph``.

    ripost.hu puts its NewsArticle inside an ``@graph`` alongside a
    BreadcrumbList, so a reader that only looks at top-level nodes finds
    nothing at all.
    """
    blocks: list[dict] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        queue = list(data) if isinstance(data, list) else [data]
        while queue:
            node = queue.pop(0)
            if not isinstance(node, dict):
                continue
            blocks.append(node)
            graph = node.get("@graph")
            if isinstance(graph, list):
                queue.extend(graph)
    return blocks


def pick_article_node(blocks: list[dict]) -> dict:
    """The NewsArticle/Article node, if the page has one."""
    for node in blocks:
        node_type = node.get("@type", "")
        types = node_type if isinstance(node_type, list) else [node_type]
        if any("Article" in str(t) for t in types):
            return node
    return {}


def _node_name(raw: Any) -> str | None:
    """JSON-LD person/organisation -> a name string."""
    if not raw:
        return None
    if isinstance(raw, str):
        return raw.strip() or None
    if isinstance(raw, dict):
        return (raw.get("name") or "").strip() or None
    return None


def normalize_authors(raw: Any) -> list[str]:
    """JSON-LD ``author`` is a string, an object with .name, or a list of
    either. Always returned as a de-duplicated list, per the article.json
    contract."""
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    names = [_node_name(item) for item in items]
    return list(dict.fromkeys(n for n in names if n))


# =====================================================================
# Plain HTML metadata
# =====================================================================

def meta_content(soup: BeautifulSoup, *candidates: tuple[str, str]) -> str | None:
    """First non-empty ``<meta>`` content among (attribute, value) pairs."""
    for attribute, value in candidates:
        tag = soup.find("meta", attrs={attribute: value})
        if tag and (tag.get("content") or "").strip():
            return tag["content"].strip()
    return None


def link_href(soup: BeautifulSoup, rel: str) -> str | None:
    tag = soup.find("link", attrs={"rel": rel})
    if tag and (tag.get("href") or "").strip():
        return tag["href"].strip()
    return None


def dom_text(soup: BeautifulSoup, selector: str) -> str | None:
    if not selector:
        return None
    try:
        node = soup.select_one(selector)
    except Exception:                 # an invalid selector is a rule bug, not a page bug
        return None
    if node is None:
        return None
    return node.get_text(" ", strip=True) or None


# =====================================================================
# Candidate resolution
# =====================================================================

@dataclass
class Resolved:
    """One field's winning value and where it came from."""
    value: Any = None
    source: str | None = None


def resolve(candidates: Iterable[tuple[str, Any]],
            accept: Callable[[Any], bool] | None = None) -> Resolved:
    """First candidate whose value is non-empty (and passes ``accept``).

    ``candidates`` is an ordered sequence of ``(source_name, value)``.
    Callables are supported so an expensive DOM query is only run if the
    cheaper sources ahead of it came up empty.
    """
    for source, value in candidates:
        if callable(value):
            value = value()
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
        if not value:
            continue
        if accept is not None and not accept(value):
            continue
        return Resolved(value=value, source=source)
    return Resolved()


# =====================================================================
# Tags
# =====================================================================

def _is_article_scoped_tag_link(anchor, rules) -> bool:
    """Is this tag-index link THIS article's tag, or site furniture?

    Walks up from the anchor. Rejects outright on a ``<nav>`` or on any
    container matching ``tag_reject_pattern``; accepts only if something
    on the way up matches ``tag_scope_pattern``. See the long note in
    ``sites/base.py`` for why the href pattern alone cannot decide this.
    """
    reject = re.compile(rules.tag_reject_pattern, re.I)
    scope = re.compile(rules.tag_scope_pattern, re.I)
    scoped = False
    node = anchor
    for _ in range(rules.tag_scope_depth):
        node = node.parent
        if node is None or node.name in ("body", "html"):
            break
        tokens = " ".join([node.name] + (node.get("class") or []))
        if node.name == "nav" or reject.search(tokens):
            return False
        if scope.search(tokens):
            scoped = True
    return scoped


def extract_tags(soup: BeautifulSoup, blocks: list[dict], article: dict,
                 url: str, rules) -> list[str]:
    """The article's own topical tags.

    **Tags are not the section.** Until 2026-08-17 this function also read
    JSON-LD ``articleSection``, the BreadcrumbList, and - as a last
    resort - the first path segment of the URL. All three are the
    *section* or the navigation path wearing a tag's clothes, and they
    are already captured as ``section``. Measured on the 50-article
    sample, that made ``tags == [section]`` for 10/10 origo, 10/10
    metropol and 9/10 pestisracok, and turned magyarnemzet's tags into
    ``['Magyar Nemzet', 'Belföld', '<the article title>']`` - a
    breadcrumb. Not one outlet produced a real tag. Those three sources
    are gone; do not reinstate them.

    Real tags are links to the tag index, scoped to this article's own
    subtree - see ``_is_article_scoped_tag_link``.
    """
    tags: list[str] = []

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not any(pattern in href for pattern in rules.tag_link_patterns):
            continue
        text = " ".join(anchor.get_text(" ", strip=True).split())
        if not text or len(text) > 60:
            continue
        if _is_article_scoped_tag_link(anchor, rules):
            tags.append(text)

    # Structured sources, kept because they are unambiguous when present.
    # None of the six outlets populates them today (measured: 0/50 have a
    # non-empty meta keywords or article:tag), but they cost nothing.
    value = article.get("keywords")
    if isinstance(value, str) and value.strip():
        tags.extend(part.strip() for part in value.split(",") if part.strip())
    elif isinstance(value, list):
        tags.extend(str(part).strip() for part in value if str(part).strip())

    keywords = soup.find("meta", attrs={"name": "keywords"})
    if keywords and (keywords.get("content") or "").strip():
        tags.extend(p.strip() for p in keywords["content"].split(",") if p.strip())

    for meta_tag in soup.find_all("meta", attrs={"property": "article:tag"}):
        if (meta_tag.get("content") or "").strip():
            tags.append(meta_tag["content"].strip())

    seen, unique = set(), []
    for tag in tags:
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(tag)
    return unique


# =====================================================================
# The builder
# =====================================================================

@dataclass
class ArticleMetadata:
    title: str | None = None
    subtitle: str | None = None
    author: list[str] = field(default_factory=list)
    publisher: str | None = None
    canonical_url: str | None = None
    source_url: str | None = None
    published_at: str | None = None
    updated_at: str | None = None
    language: str | None = None
    section: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "subtitle": self.subtitle,
            "author": self.author,
            "publisher": self.publisher,
            "canonical_url": self.canonical_url,
            "source_url": self.source_url,
            "published_at": self.published_at,
            "updated_at": self.updated_at,
            "language": self.language,
            "section": self.section,
            "description": self.description,
            "tags": self.tags,
        }


def build_metadata(soup: BeautifulSoup, url: str, rules,
                   readability_title: str | None = None,
                   readability_byline: str | None = None) -> ArticleMetadata:
    """Merge every source into one ArticleMetadata.

    ``soup`` MUST be the original, unstripped document: JSON-LD,
    OpenGraph and the canonical link all live in ``<head>``, which the
    boilerplate pass is entitled to delete.
    """
    blocks = json_ld_blocks(soup)
    article = pick_article_node(blocks)
    sources: dict[str, str] = {}

    def take(name: str, resolved: Resolved):
        if resolved.source:
            sources[name] = resolved.source
        return resolved.value

    title = take("title", resolve([
        ("jsonld", article.get("headline")),
        ("opengraph", lambda: meta_content(soup, ("property", "og:title"))),
        ("meta", lambda: meta_content(soup, ("name", "twitter:title"))),
        ("readability", readability_title),
        ("dom", lambda: soup.title.get_text(strip=True) if soup.title else None),
        ("dom", lambda: dom_text(soup, "h1")),
    ]))

    def alternative_headline():
        """``alternativeHeadline``, unless it merely repeats the headline.

        Rejecting it HERE rather than after resolution is the whole point.
        ripost.hu sets ``alternativeHeadline`` equal to ``headline`` on
        essentially every article (180/180 in a 2026-08-25 sample), and a
        candidate that resolves SHORT-CIRCUITS the list - so the real
        standfirst sitting in the DOM was never consulted, and the
        post-hoc equality check below then discarded the only value we
        had. The lead was lost for both reasons at once.
        """
        value = article.get("alternativeHeadline")
        if value and title and _same_text(value, title):
            return None
        return value

    subtitle = take("subtitle", resolve([
        ("jsonld", alternative_headline),
        ("dom", lambda: dom_text(soup, rules.subtitle_selector)),
    ]))
    # ripost.hu routinely sets alternativeHeadline equal to headline, which
    # would render the same sentence twice under itself. A subtitle that
    # merely repeats the title carries no information.
    if subtitle and title and _same_text(subtitle, title):
        subtitle = None
        sources.pop("subtitle", None)

    # Authors: JSON-LD first, but a site rule may reject an Organization
    # masquerading as a byline (see sites/ripost.py).
    jsonld_authors = [a for a in normalize_authors(article.get("author"))
                      if rules.accept_author(a)]
    author_resolved = resolve([
        ("jsonld", jsonld_authors or None),
        ("meta", lambda: meta_content(soup, ("name", "author"),
                                      ("property", "article:author"))),
        ("dom", lambda: dom_text(soup, rules.author_selector)),
        ("readability", readability_byline),
    ])
    authors = author_resolved.value or []
    if isinstance(authors, str):
        authors = [authors]
    authors = [a for a in dict.fromkeys(authors) if a and rules.accept_author(a)]
    if author_resolved.source and authors:
        sources["author"] = author_resolved.source

    publisher = take("publisher", resolve([
        ("jsonld", _node_name(article.get("publisher"))),
        ("opengraph", lambda: meta_content(soup, ("property", "og:site_name"))),
        ("site", rules.publisher),
    ]))

    canonical_url = take("canonical_url", resolve([
        ("dom", lambda: link_href(soup, "canonical")),
        ("opengraph", lambda: meta_content(soup, ("property", "og:url"))),
        ("jsonld", article.get("url")),
    ]))

    published_at = take("published_at", resolve([
        ("jsonld", article.get("datePublished")),
        ("opengraph", lambda: meta_content(soup, ("property", "article:published_time"))),
        ("meta", lambda: meta_content(soup,
                                      ("name", "article:published_time"),
                                      ("itemprop", "datePublished"),
                                      ("name", "publish-date"))),
        ("dom", lambda: _time_attr(soup)),
    ]))

    updated_at = take("updated_at", resolve([
        ("jsonld", article.get("dateModified")),
        ("opengraph", lambda: meta_content(soup, ("property", "article:modified_time"))),
        ("meta", lambda: meta_content(soup, ("itemprop", "dateModified"))),
    ]))

    language = take("language", resolve([
        ("dom", lambda: (soup.html.get("lang") if soup.html else None)),
        ("jsonld", article.get("inLanguage") if isinstance(article.get("inLanguage"), str) else None),
        ("opengraph", lambda: meta_content(soup, ("property", "og:locale"))),
        ("site", rules.default_language),
    ]))
    if isinstance(language, str):
        # 'hu-HU' / 'hu_HU' -> 'hu'; keep it a language, not a locale
        language = language.replace("_", "-").split("-")[0].lower() or None

    section = take("section", resolve([
        ("jsonld", article.get("articleSection") if isinstance(article.get("articleSection"), str) else None),
        ("opengraph", lambda: meta_content(soup, ("property", "article:section"))),
        ("url", lambda: rules.section_from_url(url)),
    ]))

    description = take("description", resolve([
        ("jsonld", article.get("description")),
        ("opengraph", lambda: meta_content(soup, ("property", "og:description"))),
        ("meta", lambda: meta_content(soup, ("name", "description"),
                                      ("name", "twitter:description"))),
    ]))

    metadata = ArticleMetadata(
        title=title,
        subtitle=subtitle,
        author=authors,
        publisher=publisher,
        canonical_url=canonical_url,
        source_url=url,
        published_at=published_at,
        updated_at=updated_at,
        language=language,
        section=section,
        description=description,
        tags=extract_tags(soup, blocks, article, url, rules),
    )
    # Which source won each field. Kept for the log only: it is genuinely
    # useful when a field looks wrong, and it is not article metadata, so it is
    # never serialised into article.json.
    metadata.resolved_sources = sources
    return metadata


def _same_text(left: str, right: str) -> bool:
    """Equality ignoring case, accents-as-written, and whitespace runs."""
    import unicodedata
    def canon(value: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()
    return canon(left) == canon(right)


def _time_attr(soup: BeautifulSoup) -> str | None:
    tag = soup.find("time")
    if tag and (tag.get("datetime") or "").strip():
        return tag["datetime"].strip()
    return None
