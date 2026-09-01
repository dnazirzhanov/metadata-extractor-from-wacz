"""
sanitize.py
===========
The allowlist sanitiser. Ported UNCHANGED from
``causalia-final/extractor/core/sanitize.py`` - this is the security contract
and it is not the place to be creative.

Threat model is script execution and NETWORK EGRESS. The predecessor
``scripts/wacz_processor.py`` set ``src`` to the original publisher URL when an
image was not present in the archive, which renders as a real request to the
publisher's CDN every time the offline page is opened. ``_safe_src`` permits
only local ``images/`` and ``videos/`` paths; anything else is moved to
``data-original-src`` and marked ``data-archive-missing``.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString

#: tag -> attributes permitted on it. Anything absent is dropped.
ALLOWED_TAGS: dict[str, frozenset[str]] = {
    "p": frozenset(),
    "br": frozenset(),
    "hr": frozenset(),
    "h1": frozenset(), "h2": frozenset(), "h3": frozenset(),
    "h4": frozenset(), "h5": frozenset(), "h6": frozenset(),
    "strong": frozenset(), "b": frozenset(),
    "em": frozenset(), "i": frozenset(), "u": frozenset(),
    "s": frozenset(), "sub": frozenset(), "sup": frozenset(),
    "small": frozenset(), "mark": frozenset(),
    "blockquote": frozenset({"cite"}),
    "q": frozenset({"cite"}),
    "ul": frozenset(), "ol": frozenset({"start"}), "li": frozenset(),
    "dl": frozenset(), "dt": frozenset(), "dd": frozenset(),
    "figure": frozenset(), "figcaption": frozenset(),
    "table": frozenset(), "thead": frozenset(), "tbody": frozenset(),
    "tfoot": frozenset(), "tr": frozenset(),
    "th": frozenset({"colspan", "rowspan", "scope"}),
    "td": frozenset({"colspan", "rowspan"}),
    "caption": frozenset(),
    "code": frozenset(), "pre": frozenset(), "kbd": frozenset(),
    "time": frozenset({"datetime"}),
    "a": frozenset({"href", "rel", "target", "title"}),
    # data-image-id / data-video-id are stamped on by the media stage and
    # are how blocks.py links a block to its entry in images.json /
    # videos.json. They must survive sanitising or content.json loses the
    # media positions the evidence model depends on.
    "img": frozenset({"src", "alt", "title", "width", "height",
                      "data-image-id", "data-original-src", "data-archive-missing"}),
    "video": frozenset({"src", "controls", "poster", "width", "height",
                        "preload",
                        "data-video-id", "data-original-src", "data-archive-missing"}),
    "source": frozenset({"src", "type"}),
    # produced by readability.restore_embeds for third-party players
    "div": frozenset({"class", "data-video-id", "data-embed-url", "data-embed-platform"}),
    "span": frozenset({"class"}),
    "section": frozenset(), "article": frozenset(),
}

#: Dropped entirely, CONTENTS AND ALL. Everything else that is unknown is
#: merely unwrapped, keeping its text.
DROP_WITH_CONTENT = frozenset({
    "script", "style", "link", "meta", "noscript", "template",
    "form", "input", "button", "select", "textarea", "label",
    "object", "embed", "applet", "iframe", "frame", "frameset",
    "audio", "canvas", "map", "area", "svg", "math", "base",
})

#: Only these schemes may appear in an href.
ALLOWED_LINK_SCHEMES = frozenset({"http", "https", "mailto"})

#: class values kept on div/span. Anything else is dropped, so publisher
#: CSS hooks cannot survive into our output and imply styling we do not
#: ship.
ALLOWED_CLASSES = frozenset({"embed", "embed-missing", "caption", "credit"})


def _safe_href(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if value.startswith("#"):
        return None                 # in-page anchors point at chrome we removed
    try:
        scheme = urlsplit(value).scheme.lower()
    except ValueError:
        return None
    if not scheme:
        return None                 # relative link out of an archived page: meaningless
    if scheme not in ALLOWED_LINK_SCHEMES:
        return None                 # javascript:, data:, tel:, blob: ...
    return value


def _safe_src(value: str) -> str | None:
    """``src`` may ONLY be a relative path into our own media directories.

    This is what guarantees the page makes no network request when opened.
    """
    value = (value or "").strip()
    if not value:
        return None
    if ".." in value:
        return None
    if value.startswith(("images/", "./images/", "videos/", "./videos/")):
        return value
    return None


def sanitize(soup: BeautifulSoup) -> BeautifulSoup:
    """Sanitise ``soup`` in place against the allowlist, and return it."""
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    for element in soup.find_all(list(DROP_WITH_CONTENT)):
        element.decompose()

    # list() because we mutate the tree while walking it
    for element in list(soup.find_all(True)):
        if element.name is None or element.attrs is None:
            continue                            # already detached

        allowed_attributes = ALLOWED_TAGS.get(element.name)
        if allowed_attributes is None:
            element.unwrap()                    # unknown tag: keep the words
            continue

        for name in list(element.attrs):
            lowered = name.lower()

            if lowered.startswith("on") or lowered in ("style", "srcset", "data-srcset"):
                del element[name]
                continue
            if lowered not in allowed_attributes:
                del element[name]
                continue

            value = element.get(name)
            if isinstance(value, list):
                value = " ".join(value)

            if lowered == "href":
                safe = _safe_href(str(value))
                if safe is None:
                    del element[name]
                else:
                    element[name] = safe
            elif lowered == "src":
                safe = _safe_src(str(value))
                if safe is None:
                    # Not one of ours: keep the provenance, kill the fetch.
                    del element[name]
                    if str(value).strip():
                        element["data-original-src"] = str(value).strip()
                    element["data-archive-missing"] = "true"
                else:
                    element[name] = safe
            elif lowered == "class":
                kept = [c for c in str(value).split() if c in ALLOWED_CLASSES]
                if kept:
                    element[name] = " ".join(kept)
                else:
                    del element[name]

        # An <a> that lost its href is no longer a link, just words.
        if element.name == "a" and not element.get("href"):
            element.unwrap()
            continue

        if element.name == "a" and element.get("href"):
            # Outbound links are allowed to exist - a link is not a request
            # until somebody clicks it - but must not leak a referrer or
            # get window.opener access.
            element["rel"] = "noopener noreferrer nofollow"
            element["target"] = "_blank"

    # Drop elements that ended up empty and carry no meaning on their own.
    for element in list(soup.find_all(["p", "figcaption", "li", "blockquote", "span"])):
        if element.name is None:
            continue
        if not element.get_text(strip=True) and not element.find(["img", "video"]):
            element.decompose()

    return soup


def sanitize_html(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    sanitize(soup)
    body = soup.body
    if body is not None:
        return body.decode_contents()
    return soup.decode()


def text_of(node) -> str:
    """Whitespace-normalised text of a node, for blocks and link context."""
    if isinstance(node, NavigableString):
        return " ".join(str(node).split())
    return " ".join(node.get_text(" ", strip=True).split())
