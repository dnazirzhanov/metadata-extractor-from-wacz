"""
readability.py
==============
Run Mozilla Readability (the ``readability-lxml`` port) over the captured
document, and keep the embeds it would otherwise destroy.

Ported from ``causalia-final/extractor/core/readability.py``.

THE EMBED PROBLEM: Readability DELETES ``<iframe>`` and ``<video>`` outright, so
every embed is swapped for a text token before Readability runs and swapped back
afterwards. ripost.hu wraps each player in ``<div class="raw-html-embed">``, and
Readability discards that div because it holds no text - taking the token with
it - so the token replaces the OUTERMOST wrapper that contains only the embed.

``readability-lxml`` rather than a Node bridge to ``@mozilla/readability``
because neither milab2 nor milab4 has Node installed, and adding a Node runtime
to run one function is not worth it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from readability import Document

#: Deliberately alphanumeric and unspaced: Readability's scoring splits on
#: punctuation and can drop a token that looks like markup or a sentence.
EMBED_TOKEN = "CAUSALIAEMBED{index}ENDEMBED"
EMBED_TOKEN_RE = re.compile(r"CAUSALIAEMBED(\d+)ENDEMBED")


@dataclass
class Embed:
    url: str
    kind: str          # 'iframe' or 'video'


@dataclass
class ReadabilityResult:
    content_html: str
    title: str | None
    byline: str | None
    embeds: dict[int, Embed]


def embed_anchor(node):
    """The outermost wrapper that contains ONLY this embed.

    Walks up while each parent holds no other text, stopping before
    body/html/article/main so we never replace the whole document.
    """
    anchor = node
    parent = node.parent
    while parent is not None and parent.name not in ("body", "html", "article", "main"):
        if parent.get_text(strip=True):     # wrapper holds other content: stop
            break
        anchor = parent
        parent = parent.parent
    return anchor


def protect_embeds(soup: BeautifulSoup) -> dict[int, Embed]:
    """Replace embeds with tokens Readability will carry through.

    Lazy-loaded players leave ``src=""`` and put the real URL in
    ``data-src``, so data-src is checked first.
    """
    embeds: dict[int, Embed] = {}
    index = 0

    for frame in soup.find_all("iframe"):
        url = (frame.get("data-src") or frame.get("src") or "").strip()
        if not url or url.startswith("data:"):
            frame.decompose()
            continue
        index += 1
        embeds[index] = Embed(url=url, kind="iframe")
        placeholder = soup.new_tag("p")
        placeholder.string = EMBED_TOKEN.format(index=index)
        embed_anchor(frame).replace_with(placeholder)

    for video in soup.find_all("video"):
        url = (video.get("src") or "").strip()
        if not url:
            source = video.find("source")
            if source:
                url = (source.get("src") or source.get("data-src") or "").strip()
        if not url:
            video.decompose()
            continue
        index += 1
        embeds[index] = Embed(url=url, kind="video")
        placeholder = soup.new_tag("p")
        placeholder.string = EMBED_TOKEN.format(index=index)
        embed_anchor(video).replace_with(placeholder)

    return embeds


def restore_embeds(soup: BeautifulSoup, embeds: dict[int, Embed],
                   base_url: str) -> list[Embed]:
    """Turn surviving tokens back into real elements, in place.

    A ``<video>`` becomes a real ``<video>`` (the image/media stage may
    then localise it if the bytes are in the archive). An ``<iframe>``
    becomes a marked ``<div data-embed-url=...>``: we cannot inline a
    third-party player, and re-embedding it would make an *offline* page
    phone out to YouTube the moment somebody opens it.
    """
    restored: list[Embed] = []

    for node in list(soup.find_all(string=EMBED_TOKEN_RE)):
        text = str(node)
        match = EMBED_TOKEN_RE.search(text)
        if not match:
            continue
        embed = embeds.get(int(match.group(1)))
        if embed is None:
            node.replace_with(text.replace(match.group(0), ""))
            continue

        absolute = urljoin(base_url, embed.url)
        if embed.kind == "video":
            element = soup.new_tag("video", src=absolute)
            element["controls"] = "controls"
        else:
            element = soup.new_tag("div")
            element["class"] = "embed"
            element["data-embed-url"] = absolute

        target = node.parent if node.parent and node.parent.name == "p" else node
        target.replace_with(element)
        restored.append(embed)

    return restored


def adopt_raw_embeds(soup: BeautifulSoup, base_url: str) -> list[Embed]:
    """Convert RAW ``<iframe>``/``<video>`` into the shapes restore_embeds makes.

    ``protect_embeds`` runs on the SERVED html, so its tokens only ever
    exist inside Readability's output. When the ng-state fallback in
    ``pipeline`` replaces that body, the replacement is the CMS's own
    markup, carrying unprotected ``<iframe>``/``<video>`` that no token
    ever stood for. ``VideoExtractor`` looks for ``data-embed-url`` and
    ``<video>``, so it never sees a bare iframe, and ``sanitize`` then
    deletes it outright because ``iframe`` is not in ``ALLOWED_TAGS``.

    Measured 2026-08-24 on magyarnemzet, whose body almost always comes
    from ng-state: the YouTube, Facebook and Instagram embeds were all
    present in the recovered body and all silently discarded. The URL was
    in our hands the whole time.

    Idempotent by construction: on the normal path every iframe is
    already a token and every restored ``<video>`` already absolute, so
    there is nothing to adopt and this returns an empty list. Run it
    BEFORE ``restore_embeds`` so the two never contend for one element.
    """
    adopted: list[Embed] = []

    for frame in list(soup.find_all("iframe")):
        # Same precedence as protect_embeds: lazy players leave src empty
        # and put the real URL in data-src.
        raw = (frame.get("data-src") or frame.get("src") or "").strip()
        if not raw or raw.startswith("data:"):
            frame.decompose()
            continue
        absolute = urljoin(base_url, raw)
        element = soup.new_tag("div")
        element["class"] = "embed"
        element["data-embed-url"] = absolute
        embed_anchor(frame).replace_with(element)
        adopted.append(Embed(url=absolute, kind="iframe"))

    for video in list(soup.find_all("video")):
        raw = (video.get("src") or "").strip()
        if not raw:
            source = video.find("source")
            if source is not None:
                raw = (source.get("src") or source.get("data-src") or "").strip()
        if not raw or raw.startswith("data:"):
            # Leave it alone rather than decomposing: VideoExtractor still
            # records a src-less <video>, and dropping it here would lose
            # the only evidence the article had one.
            continue
        absolute = urljoin(base_url, raw)
        video["src"] = absolute
        video["controls"] = "controls"
        adopted.append(Embed(url=absolute, kind="video"))

    return adopted


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _has_content(node) -> bool:
    """Does this block carry article substance - prose or a picture?"""
    if node.find("img") is not None:
        return True
    return len(_norm(node.get_text(" ", strip=True))) >= 40


def recover_sibling_blocks(source_html: str, content_html: str) -> str:
    """Re-attach body blocks Readability left behind.

    Readability returns ONE container: the single highest-scoring node.
    That is right for a page whose article is one div, and wrong for a
    CMS that splits the body into sibling blocks - metropol.hu emits
    several ``div.block-content`` in a row, and everything after the
    first is silently dropped.

    Measured 2026-08-17 on ``metropol.hu/vip/2021/06/szemelyisegzavar-arpa-attila-narcisztikus``:
    the stripped document holds 18,198 bytes over two ``div.block-content``
    siblings (2,298 chars of prose; then 145 chars plus the closing
    photo). Readability returned 2,546 bytes and 0 images - the trailing
    paragraph and the article's own picture were gone, and because they
    never reached the image pass, ``images_missing`` was 0: the artifact
    did not even record a loss.

    The rule here is deliberately narrow, because widening it is how a
    body extractor starts swallowing furniture: re-attach a sibling ONLY
    if it carries the *same class attribute* as the container Readability
    chose, and only if it holds prose or an image. Recommendation rails,
    newsletter boxes and galleries all carry different classes, so they
    stay out. If nothing matches, the output is returned untouched.

    Note that removing the surrounding furniture does NOT fix this on its
    own - tried, and Readability returned byte-identical output. The
    sibling has to be re-attached explicitly.
    """
    body_text = _norm(BeautifulSoup(content_html, "lxml").get_text(" ", strip=True))
    if len(body_text) < 40:
        return content_html
    probe = body_text[:60]

    source = BeautifulSoup(source_html, "lxml")
    # The chosen container is the smallest element that still holds the
    # WHOLE of Readability's output. Matching on the opening text alone
    # finds the first <p> instead - it starts with the same words, has no
    # class, and the recovery then silently does nothing.
    # Ties are the norm, not the exception: a wrapper and the div inside
    # it hold exactly the same text (metropol nests div.block-content in
    # <metropol-wysiwyg-box>). Prefer the DEEPEST of an equal-length set -
    # keeping the outer one picks an element with no class attribute and
    # the recovery then bails without doing anything.
    chosen = chosen_len = chosen_depth = None
    for element in source.find_all(True):
        text = _norm(element.get_text(" ", strip=True))
        if not text.startswith(probe) or len(text) < len(body_text) * 0.9:
            continue
        depth = len(list(element.parents))
        if (chosen is None or len(text) < chosen_len
                or (len(text) == chosen_len and depth > chosen_depth)):
            chosen, chosen_len, chosen_depth = element, len(text), depth
    if chosen is None or chosen.parent is None:
        return content_html

    signature = chosen.get("class")
    if not signature:
        return content_html

    # The peer blocks are usually NOT immediate siblings: metropol wraps
    # each one in its own <metropol-wysiwyg-box>, so they are cousins.
    # Walk up until we reach the first ancestor that contains more than
    # one block of this class - that ancestor is the article body.
    container, peers = chosen.parent, None
    for _ in range(6):
        if container is None:
            break
        same = [e for e in container.find_all(True) if e.get("class") == signature]
        if len(same) > 1:
            peers = same
            break
        container = container.parent
    if not peers:
        return content_html

    ordered, found_extra = [], False
    for block in peers:                       # find_all preserves document order
        if block is chosen:
            ordered.append(content_html)
        elif _has_content(block):
            ordered.append(str(block))
            found_extra = True
    if not found_extra:
        return content_html
    return "".join(ordered)


def run_readability(html: str, url: str) -> ReadabilityResult:
    """Protect embeds, run Readability, and hand back its body and title.

    Raises whatever Readability raises; the caller turns that into a
    per-article failure rather than letting it stop the batch.
    """
    protected = BeautifulSoup(html, "lxml")
    embeds = protect_embeds(protected)
    source = str(protected) if embeds else html

    document = Document(source, url=url)
    content_html = document.summary(html_partial=True)
    content_html = recover_sibling_blocks(source, content_html)
    try:
        title = document.short_title()
    except Exception:
        title = None
    try:
        byline = document.author()
    except Exception:
        byline = None

    # readability-lxml returns the sentinel string "[no-author]" rather
    # than None when it cannot find a byline. Passing that through would
    # write a literal "[no-author]" into article.json as if it were a
    # person's name.
    if isinstance(byline, str) and byline.strip().lower() in ("", "[no-author]"):
        byline = None

    return ReadabilityResult(
        content_html=content_html,
        title=title.strip() if isinstance(title, str) and title.strip() else None,
        byline=byline.strip() if isinstance(byline, str) and byline.strip() else None,
        embeds=embeds,
    )
