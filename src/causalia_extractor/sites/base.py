# Ported from causalia-final/extractor/sites/base.py
"""
extractor/sites/base.py
=======================
The generic extraction rules - what we assume about a news site when we
know nothing specific about it.

Every hook here has a sane default, so an outlet with no rules file of its
own still extracts. That matters: ripost.hu is the first outlet through
this pipeline, but origo.hu, magyarnemzet.hu, mandiner.hu, metropol.hu and
pestisracok.hu are all coming, and none of them should need code before
they can be tried.

Site rules exist to hold facts that are true of ONE publisher. Nothing in
``extractor/core`` may import from here except through the registry.
"""

from __future__ import annotations

from urllib.parse import urlparse

from bs4 import BeautifulSoup


class SiteRules:
    """Default behaviour. Subclass and override only what differs."""

    #: Hostname this rule set claims, e.g. "ripost.hu". None = the default.
    outlet: str | None = None

    #: Shown in article.json when the page itself does not say.
    publisher: str | None = None
    site_name: str | None = None
    default_language: str | None = None

    #: CSS selectors tried only after the structured sources come up empty.
    subtitle_selector: str = ".lead, .sub-title, .subtitle, .article-lead, h2.lead"
    author_selector: str = ".author-name, .author, [rel=author], [itemprop=author]"

    #: Substrings marking an image as site furniture rather than content -
    #: logos, share placeholders, spacer gifs.
    generic_image_markers: tuple[str, ...] = (
        "/logo", "placeholder", "default-share", "sprite", "1x1", "pixel",
    )

    # -- tags ---------------------------------------------------------
    # A tag is a TOPICAL label the publisher attached to THIS article. It
    # is not the section (where the article is filed - a separate field),
    # not the breadcrumb, and not the site's trending list. On every
    # outlet in this corpus the only reliable marker is a link to the tag
    # index; Hungarian sites spell that "cimke".
    #
    # The hard part is that the SAME href pattern appears in three places
    # that are not this article's tags: the site header/trending strip,
    # the hamburger menu, and the tag chips printed on recommended
    # article CARDS. Measured 2026-08-17: 82-222 such links per page
    # against 0-18 real ones. So a pattern match alone is not enough -
    # the anchor must also be inside the article's own subtree, and not
    # inside a nav/menu/card/trending/opinion container.
    tag_link_patterns: tuple[str, ...] = (
        "/cimke/", "/cimkek/", "/tag/", "/tags/", "/temak/", "/topic/",
    )
    #: An ancestor whose element name or class matches this is what makes
    #: an anchor "this article's". Every outlet wraps its tag block in
    #: something article-ish: div.article-tags (origo, pestisracok),
    #: div.article-header (magyarnemzet, ripost), metropol-article-header.
    tag_scope_pattern: str = r"article"
    #: ...but reject these even when article-scoped, because recommended
    #: cards live inside the article page too. Every word here describes a
    #: container that is about OTHER content, whatever it sits inside.
    #:
    #: ``trending`` was in this list and was removed 2026-08-31. It is the one
    #: word that describes PROMINENCE rather than ownership, and mandiner.hu
    #: uses it for the article's own tag row: those tags live in
    #: ``div.trending-topics < man-trending-topics < div.wrapper.with-aside <
    #: section.article-page``, so rejecting the word cost mandiner its tags on
    #: every article. Its site-wide strip is separated by the scope test
    #: instead - that one sits in ``div.header-hamburger-menu-left``, which
    #: ``menu`` still rejects, and reaches no ``article`` ancestor.
    #:
    #: Measured before changing it, over 40 articles from 8 outlets: mandiner
    #: gained tags on 5 of 5 (0 -> 3..10, each set topical and different, so not
    #: a repeating site strip); metropol, origo, ripost, magyarnemzet,
    #: pestisracok, bama and heol were unchanged on 35 of 35. Do not put
    #: ``trending`` back without re-running that comparison.
    tag_reject_pattern: str = (
        r"(menu|card|opinion|related|recommend|popular|sidebar|widget)"
    )
    #: How far up to look for the two patterns above.
    tag_scope_depth: int = 8

    #: Tags the boilerplate pass must NOT delete for this site. Some
    #: layouts put the article's own hero inside <header>.
    keep_furniture_tags: frozenset[str] = frozenset()

    #: Phrases that identify a captured page as an INTERSTITIAL rather
    #: than the article - the crawler got a gate, not the content.
    #:
    #: This matters more than it looks. Such a capture still carries the
    #: article's JSON-LD and OpenGraph tags, so title, date, section and
    #: description all extract perfectly while the body is absent. It is
    #: the same class of silent failure as the 5xx-archived-as-success bug
    #: recorded in HANDOFF.md section 3, and it is equally invisible to a
    #: WACZ size check: the observed example is 1.36 MB, far above the
    #: 512 kB floor that detects error bodies.
    #:
    #: Matched accent-insensitively against the page text. Hungarian
    #: phrases are shared across the Mediaworks outlets in this corpus,
    #: so they live here rather than in one site's rules.
    interstitial_markers: tuple[tuple[str, str], ...] = (
        ("kiskorúakra károsak lehetnek", "age-gate"),
        ("elmúltam 18 éves", "age-gate"),
        ("nem múltam el 18 éves", "age-gate"),
    )

    # -- hooks -------------------------------------------------------

    def accept_author(self, name: str) -> bool:
        """Is this string a usable byline?

        Rejects the obvious non-answers extractors like to produce. A site
        rule can narrow this further.
        """
        cleaned = (name or "").strip()
        if len(cleaned) < 2 or len(cleaned) > 120:
            return False
        return cleaned.lower() not in ("null", "none", "unknown", "admin", "-")

    def section_from_url(self, url: str) -> str | None:
        """Section from the URL path.

        The corpus-wide convention is ``/<section>/<yyyy>/<mm>/<slug>``,
        which is also the shape the archiver's claim filter keys on, so
        the first non-numeric segment is a reliable section.
        """
        segments = [s for s in urlparse(url).path.split("/") if s]
        if segments and not segments[0].isdigit():
            return segments[0]
        return None

    def detect_interstitial(self, page_text: str) -> str | None:
        """Name the interstitial this capture shows, or None.

        Only consulted when extraction produced no article body, so a
        normal article that merely *mentions* an age gate is never
        misclassified.
        """
        from ..boilerplate import fold
        folded = fold(page_text or "")
        for phrase, name in self.interstitial_markers:
            if fold(phrase) in folded:
                return name
        return None

    def is_generic_image(self, url: str) -> bool:
        lowered = (url or "").lower()
        return any(marker in lowered for marker in self.generic_image_markers)

    def image_identity(self, url: str) -> str:
        """A key identifying the SOURCE image behind a URL.

        Two URLs that differ only in CDN resize parameters are the same
        photograph and should compare equal. The default cannot know how
        a given CDN encodes that, so it falls back to the URL itself; a
        site rule that does know overrides this.
        """
        return (url or "").strip()

    def lead_image(self, soup: BeautifulSoup, article_node: dict) -> str | None:
        """The article's own main photo, if it can be identified.

        Readability's body block does not always contain the hero image -
        many layouts put it in a sibling component - so a body-only scrape
        can produce an illustrated article with no illustration. JSON-LD
        ``image`` is the trustworthy source; ``og:image`` is a fallback and
        is rejected when it points at a site-wide placeholder.
        """
        candidate = article_node.get("image")
        if isinstance(candidate, dict):
            candidate = candidate.get("url")
        elif isinstance(candidate, list):
            candidate = next((item.get("url") if isinstance(item, dict) else item
                              for item in candidate if item), None)

        if not candidate:
            tag = (soup.find("meta", attrs={"property": "og:image"})
                   or soup.find("meta", attrs={"name": "twitter:image"}))
            candidate = (tag.get("content") if tag else None)

        if not isinstance(candidate, str) or not candidate.strip():
            return None
        candidate = candidate.strip()
        if self.is_generic_image(candidate):
            return None
        return candidate
