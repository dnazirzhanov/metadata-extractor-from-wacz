"""
dom.py
======
Build the canonical document, write it, read it back.

``readability.html`` is the coordinate system for every content reference
Causalia stores, so its structure is OURS, not the publisher's. This module
turns the messy sanitised Readability output into a controlled document::

    /html/body/article/header/h1              title      (metadata, not a block)
    /html/body/article/header/p               subtitle   (metadata, not a block)
    /html/body/article/div/figure[1]/img      image block
    /html/body/article/div/p[1]               paragraph block
    /html/body/article/div/p[2]               paragraph block
    /html/body/article/div/h2[1]              heading block
    /html/body/article/div/blockquote[1]      quote block
    /html/body/article/div/ul[1]/li[1]        list item

Every content block is a direct child of one container. Inline markup
(``strong``, ``em``, ``a``, ``span``, ``br``) is preserved inside blocks,
because that is what makes character offsets meaningful. What is flattened away
is the publisher's structural nesting, which varies per outlet and per article
and would otherwise put an identical paragraph at ``div/div/p[3]`` on one site
and ``div/span/div/p[1]`` on another.

WHITESPACE BETWEEN BLOCKS IS LOAD-BEARING
-----------------------------------------
Block-level elements are serialised with a newline between them. That newline is
a real text node, so a browser's ``textContent`` includes it - which means
``normalize_text`` of a ``<ul>`` yields ``"first second"`` rather than
``"firstsecond"``, with no special-casing anywhere and no divergence between our
Python and the frontend's JavaScript. Inline content is never reformatted, so no
whitespace is ever introduced inside a paragraph.

THE ROUND TRIP
--------------
``build`` produces the tree and the ordered block specs. ``serialize`` renders
it. ``reparse`` reads those exact bytes back, and ``walk_blocks`` re-derives the
block elements from the re-parsed tree by a trivial, deterministic rule - every
element child of the body container is a block. XPaths are generated against
THAT tree.

This matters because HTML serialisation can move nodes: an implied ``<tbody>``,
a misnested inline element. An XPath computed before serialisation can be a
correct description of a tree that was never written to disk. Generating after
the round trip makes "this XPath resolves against this file" true by
construction, and the spec/element count check in ``walk_blocks`` turns any
structural surprise into a loud failure rather than a silently wrong path.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import lxml.html as LH
from lxml import etree

from .boilerplate import is_boilerplate_line
from .models import HEADING, IMAGE, LIST, PARAGRAPH, QUOTE, VIDEO
from .normalize import collapse, normalize_text

log = logging.getLogger(__name__)

#: Class of the single container holding every content block.
BODY_CLASS = "article-body"

#: Tags that may survive inside a block as inline content. Anything else found
#: at inline position is unwrapped, keeping its text.
INLINE_TAGS = frozenset({
    "a", "b", "br", "code", "em", "i", "kbd", "mark", "q", "s", "small",
    "span", "strong", "sub", "sup", "time", "u",
})

_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")

#: Block-level tags we know how to turn into a content block. Anything else is
#: recursed into; a leaf we do not understand is reported, never dropped
#: silently.
_UNSUPPORTED_BLOCK_TAGS = frozenset({"table", "pre", "dl"})


@dataclass
class BlockSpec:
    """What a block is, before it has an XPath."""

    type: str
    level: int | None = None
    image_id: str | None = None
    video_id: str | None = None


@dataclass
class BuildResult:
    tree: etree._ElementTree
    specs: list[BlockSpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reading the messy input
# ---------------------------------------------------------------------------

def _bs4_to_lxml(tag):
    """One sanitised bs4 element -> an lxml element, inline content intact."""
    try:
        return LH.fragment_fromstring(str(tag))
    except (etree.ParserError, ValueError):
        return None


def _strip_to_inline(element) -> None:
    """Unwrap everything inside ``element`` that is not permitted inline markup.

    A ``<div>`` that survived inside a paragraph becomes its own text. Nothing
    is deleted - dropping a wrapper must never drop the words it contained.

    A dropped element gets a newline tail first, because unwrapping is where
    two separate runs of text become adjacent. Without it a blockquote holding
    ``<p>First.</p><p>Second.</p>`` would normalise to ``First.Second.`` - the
    same class of defect as the inline-separator bug that ``normalize.py``
    exists to prevent, just from the opposite direction.
    """
    for child in list(element.iter()):
        if child is element or not isinstance(child.tag, str):
            continue
        if child.tag.lower() not in INLINE_TAGS:
            child.tail = "\n" + (child.tail or "")
            child.drop_tag()


def _text_is_empty(element) -> bool:
    return not normalize_text(element)


#: A run of links with no prose around them is a tag strip, a breadcrumb or a
#: related-article row - furniture that happens to sit inside what Readability
#: returned as the body. Measured on mandiner.hu ``d1563b55``: the article's own
#: tag row lives in ``div.trending-topics`` INSIDE ``section.article-page``, so
#: it survives furniture stripping and Readability keeps it, and it was being
#: stored as a paragraph of article prose - which puts nine tag names into
#: full-text search and lets a citation point at them.
#:
#: The test is deliberately narrow, because a paragraph that is one long link is
#: ordinary prose:
#:   * at least two anchors - a strip is a LIST of links, one link is a sentence;
#:   * anchor text covers most of the block, so "Mint megirtuk: <a>...</a>" stays;
#:   * no sentence-ending punctuation anywhere, so a linked sentence stays.
LINK_STRIP_MIN_ANCHORS = 2
LINK_STRIP_COVERAGE = 0.8
_SENTENCE_END = ".!?" + "\u2026"


def _is_link_strip(element) -> bool:
    """True when this block is a run of links rather than prose."""
    anchors = element.xpath(".//a")
    if len(anchors) < LINK_STRIP_MIN_ANCHORS:
        return False
    text = normalize_text(element)
    if not text or any(char in text for char in _SENTENCE_END):
        return False
    linked = sum(len(normalize_text(anchor)) for anchor in anchors)
    return linked / len(text) >= LINK_STRIP_COVERAGE


# ---------------------------------------------------------------------------
# Building the canonical body
# ---------------------------------------------------------------------------

class _Builder:
    """Walks the sanitised reader soup and appends canonical blocks."""

    def __init__(self, container):
        self.container = container
        self.specs: list[BlockSpec] = []
        self.warnings: list[str] = []
        self._seen_unsupported: set[str] = set()

    # -- appending -----------------------------------------------------
    def _append(self, element, spec: BlockSpec) -> None:
        # The newline tail is what separates one block's text from the next
        # in textContent. See the module docstring.
        element.tail = "\n"
        self.container.append(element)
        self.specs.append(spec)

    def _append_text_block(self, tag, kind: str, *, level: int | None = None) -> None:
        element = _bs4_to_lxml(tag)
        if element is None:
            return
        _strip_to_inline(element)
        text = normalize_text(element)
        if not text or is_boilerplate_line(text):
            return
        if kind in (PARAGRAPH, QUOTE) and _is_link_strip(element):
            # Furniture, not a defect: logged rather than warned, so an ordinary
            # tag strip does not mark a third of the corpus `partial`.
            log.info("dropped a link strip from the body: %.80s", text)
            return
        if kind == HEADING:
            # The article title is the header's h1; a body heading is never h1.
            element.tag = "h%d" % max(2, min(6, level or 2))
        else:
            element.tag = {PARAGRAPH: "p", QUOTE: "blockquote"}[kind]
        self._append(element, BlockSpec(type=kind, level=(
            int(element.tag[1]) if kind == HEADING else None)))

    def _append_image(self, img_tag, caption: str | None) -> None:
        image_id = img_tag.get("data-image-id")
        figure = etree.Element("figure")
        image = etree.SubElement(figure, "img")
        for name in ("src", "alt", "width", "height", "data-image-id",
                     "data-original-src", "data-archive-missing"):
            value = img_tag.get(name)
            if value:
                image.set(name, str(value))
        image.tail = "\n"
        if caption:
            figcaption = etree.SubElement(figure, "figcaption")
            figcaption.text = caption
            figcaption.tail = "\n"
        figure.text = "\n"
        self._append(figure, BlockSpec(type=IMAGE, image_id=image_id))

    def _append_video(self, tag, caption: str | None) -> None:
        video_id = tag.get("data-video-id")
        figure = etree.Element("figure")
        if tag.name == "video":
            media = etree.SubElement(figure, "video")
            media.set("controls", "controls")
            for name in ("src", "poster", "width", "height", "data-video-id",
                         "data-original-src", "data-archive-missing"):
                value = tag.get(name)
                if value:
                    media.set(name, str(value))
        else:
            # A third-party player. It is never re-embedded: an offline page
            # must not phone out to YouTube the moment somebody opens it.
            media = etree.SubElement(figure, "div")
            media.set("class", "embed")
            for name in ("data-embed-url", "data-embed-platform", "data-video-id"):
                value = tag.get(name)
                if value:
                    media.set(name, str(value))
            link_url = tag.get("data-embed-url")
            if link_url:
                anchor = etree.SubElement(media, "a")
                anchor.set("href", str(link_url))
                anchor.set("rel", "noopener noreferrer nofollow")
                anchor.set("target", "_blank")
                platform = tag.get("data-embed-platform") or "video"
                anchor.text = "%s: %s" % (platform, link_url)
        media.tail = "\n"
        if caption:
            figcaption = etree.SubElement(figure, "figcaption")
            figcaption.text = caption
            figcaption.tail = "\n"
        figure.text = "\n"
        self._append(figure, BlockSpec(type=VIDEO, video_id=video_id))

    def _append_list(self, tag) -> None:
        element = etree.Element("ol" if tag.name == "ol" else "ul")
        element.text = "\n"
        kept = 0
        for li in tag.find_all("li", recursive=False):
            item = _bs4_to_lxml(li)
            if item is None:
                continue
            _strip_to_inline(item)
            item.tag = "li"
            if _text_is_empty(item):
                continue
            item.tail = "\n"
            element.append(item)
            kept += 1
        if not kept:
            return
        self._append(element, BlockSpec(type=LIST))

    # -- walking -------------------------------------------------------
    def _caption_for(self, figure_tag) -> str | None:
        caption = figure_tag.find("figcaption") if figure_tag is not None else None
        return collapse(caption.get_text()) if caption is not None else None

    def visit(self, node) -> None:
        for child in list(getattr(node, "children", []) or []):
            name = getattr(child, "name", None)
            if name is None:
                continue
            self._visit_tag(child)

    def _visit_tag(self, tag) -> None:
        name = tag.name.lower()

        if name in _HEADING_TAGS:
            self._append_text_block(tag, HEADING, level=int(name[1]))
            return

        if name == "p":
            # Media nested in a paragraph becomes its own block; the paragraph
            # keeps its words. Text first, then the media it contained.
            media = tag.find_all(["img", "video"]) + tag.find_all(
                "div", attrs={"data-embed-url": True})
            for element in media:
                element.extract()
            self._append_text_block(tag, PARAGRAPH)
            for element in media:
                self._emit_media(element, None)
            return

        if name == "blockquote":
            self._append_text_block(tag, QUOTE)
            return

        if name in ("ul", "ol"):
            self._append_list(tag)
            return

        if name == "figure":
            caption = self._caption_for(tag)
            media = (tag.find("img") or tag.find("video")
                     or tag.find("div", attrs={"data-embed-url": True}))
            if media is not None:
                self._emit_media(media, caption)
            else:
                self.visit(tag)
            return

        if name in ("img", "video"):
            self._emit_media(tag, None)
            return

        if name == "div" and tag.get("data-embed-url"):
            self._emit_media(tag, None)
            return

        if name in _UNSUPPORTED_BLOCK_TAGS:
            text = collapse(tag.get_text())
            if text and name not in self._seen_unsupported:
                self._seen_unsupported.add(name)
                self.warnings.append(
                    f"<{name}> holds {len(text)} characters that no supported "
                    f"block type can represent; its text is not in content.json")
            return

        # A structural wrapper: descend. This is where the publisher's nesting
        # is discarded and ours takes over.
        self.visit(tag)

    def _emit_media(self, tag, caption: str | None) -> None:
        if tag.name == "img":
            self._append_image(tag, caption)
        else:
            self._append_video(tag, caption)


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------

STYLE = """
:root { color-scheme: light dark; }
body { font: 18px/1.65 Georgia, 'Iowan Old Style', 'Times New Roman', serif;
       max-width: 42rem; margin: 0 auto; padding: 2.5rem 1.25rem 5rem;
       color: #1b1b1b; background: #fdfdfc; }
header { border-bottom: 1px solid #e2e2df; padding-bottom: 1.25rem;
         margin-bottom: 1.75rem; }
h1 { font-size: 1.9rem; line-height: 1.25; margin: 0 0 .6rem; }
h2, h3, h4, h5, h6 { line-height: 1.3; margin: 2rem 0 .6rem; }
.subtitle { font-size: 1.15rem; color: #55554f; margin: 0 0 .75rem;
            font-style: italic; }
.byline, .tags { font: 14px/1.5 system-ui, sans-serif; color: #6b6b66; }
.tags { margin-top: .5rem; font-size: 12px; color: #8a8a84; }
.tags span { display: inline-block; border: 1px solid #dcdcd6;
             border-radius: 999px; padding: .1rem .5rem; margin: 0 .25rem .25rem 0; }
figure { margin: 1.75rem 0; }
figure img, figure video { max-width: 100%; height: auto; display: block; }
figcaption { font: 14px/1.5 system-ui, sans-serif; color: #6b6b66;
             margin-top: .4rem; }
blockquote { margin: 1.5rem 0; padding-left: 1rem;
             border-left: 3px solid #dcdcd6; color: #444; }
.embed { border: 1px dashed #c8c8c2; padding: 1rem; font: 14px/1.5 system-ui, sans-serif; }
[data-archive-missing] { outline: 1px dashed #c8c8c2; min-height: 3rem; }
[data-archive-missing]::after { content: 'not captured in archive';
    display: block; font: 13px/1.5 system-ui, sans-serif; color: #8a8a84; }
footer { margin-top: 3rem; padding-top: 1.25rem; border-top: 1px solid #e2e2df;
         font: 13px/1.6 system-ui, sans-serif; color: #8a8a84; }
@media (prefers-color-scheme: dark) {
  body { color: #e6e6e1; background: #16161a; }
  .subtitle { color: #a9a9a2; }
  blockquote { color: #c3c3bd; border-left-color: #3a3a40; }
  header, footer { border-color: #33333a; }
}
""".strip()


def build(reader_soup, *, metadata, wacz_name: str) -> BuildResult:
    """Assemble the canonical document from the sanitised reader body."""
    html = etree.Element("html")
    if metadata.language:
        html.set("lang", metadata.language)
    html.text = "\n"

    head = etree.SubElement(html, "head")
    head.text = "\n"
    head.tail = "\n"
    for attrs in ({"charset": "utf-8"},
                  {"name": "viewport",
                   "content": "width=device-width, initial-scale=1"},
                  {"name": "referrer", "content": "no-referrer"}):
        meta = etree.SubElement(head, "meta")
        for key, value in attrs.items():
            meta.set(key, value)
        meta.tail = "\n"
    title = etree.SubElement(head, "title")
    title.text = metadata.title or ""
    title.tail = "\n"
    style = etree.SubElement(head, "style")
    style.text = STYLE
    style.tail = "\n"

    body = etree.SubElement(html, "body")
    body.text = "\n"

    article = etree.SubElement(body, "article")
    article.text = "\n"
    article.tail = "\n"

    header = etree.SubElement(article, "header")
    header.text = "\n"
    header.tail = "\n"
    heading = etree.SubElement(header, "h1")
    heading.text = metadata.title or ""
    heading.tail = "\n"
    if metadata.subtitle:
        subtitle = etree.SubElement(header, "p")
        subtitle.set("class", "subtitle")
        subtitle.text = metadata.subtitle
        subtitle.tail = "\n"
    byline_text = " - ".join(
        part for part in (", ".join(metadata.author or []),
                          metadata.published_at or "") if part)
    if byline_text:
        byline = etree.SubElement(header, "div")
        byline.set("class", "byline")
        byline.text = byline_text
        byline.tail = "\n"
    if metadata.tags:
        tags = etree.SubElement(header, "div")
        tags.set("class", "tags")
        for tag_text in metadata.tags:
            span = etree.SubElement(tags, "span")
            span.text = tag_text
        tags.tail = "\n"

    container = etree.SubElement(article, "div")
    container.set("class", BODY_CLASS)
    container.text = "\n"
    container.tail = "\n"

    builder = _Builder(container)
    if reader_soup is not None:
        builder.visit(reader_soup.body if reader_soup.body is not None else reader_soup)

    footer = etree.SubElement(body, "footer")
    footer.text = (
        "Reader view extracted offline from a web archive (%s). "
        "Images are served from ./images/; embedded players are shown as links "
        "and are never loaded automatically. Original URL: " % wacz_name)
    footer.tail = "\n"
    if metadata.source_url:
        link = etree.SubElement(footer, "a")
        link.set("href", metadata.source_url)
        link.set("rel", "noopener noreferrer nofollow")
        link.text = metadata.source_url

    return BuildResult(tree=etree.ElementTree(html), specs=builder.specs,
                       warnings=builder.warnings)


def serialize(tree) -> str:
    """The exact bytes that go to readability.html."""
    body = etree.tostring(tree, method="html", encoding="unicode")
    return "<!DOCTYPE html>\n" + body + "\n"


def reparse(html_text: str):
    """Read serialised readability.html back as the tree XPaths are made against."""
    return LH.document_fromstring(html_text).getroottree()


def body_container(tree):
    """The single element holding every content block."""
    found = tree.xpath("/html/body/article/div[@class='%s']" % BODY_CLASS)
    return found[0] if found else None


def walk_blocks(tree):
    """Re-derive the block elements from a re-parsed canonical document.

    Deterministic by construction: every element child of the body container is
    one block, and a ``<figure>`` delegates to the media element it wraps.
    Returns ``[(type, element), ...]`` in document order.
    """
    container = body_container(tree)
    if container is None:
        return []
    found: list[tuple[str, object]] = []
    for child in container:
        if not isinstance(child.tag, str):
            continue
        tag = child.tag.lower()
        if tag == "p":
            found.append((PARAGRAPH, child))
        elif re.fullmatch(r"h[1-6]", tag):
            found.append((HEADING, child))
        elif tag == "blockquote":
            found.append((QUOTE, child))
        elif tag in ("ul", "ol"):
            found.append((LIST, child))
        elif tag == "figure":
            media = child.find("img")
            if media is not None:
                found.append((IMAGE, media))
                continue
            media = child.find("video")
            if media is None:
                media = child.find("div")
            if media is not None:
                found.append((VIDEO, media))
    return found
