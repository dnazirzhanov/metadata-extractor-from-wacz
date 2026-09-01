"""URL normalisation and media-attribute helpers.

Ported verbatim from ``causalia-final/extractor/core/urls.py`` (which in turn
came from ``scripts/wacz_processor.py``). The comments explaining WHY each rule
is shaped the way it is are the valuable part - they cost debugging time to
learn the first time, and nothing about the new data contract changes them.
"""
from __future__ import annotations

import mimetypes
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit

# Everything we refuse to read out of an archive, by extension and by
# content type. Skipping these before buffering the body matters: on a
# 6 MB ripost archive they are most of the bytes and we throw them away.
SKIP_EXTENSIONS = {".css", ".js", ".mjs", ".woff", ".woff2", ".ttf", ".eot", ".otf"}
SKIP_CONTENT_TYPES = ("text/css", "javascript", "font/", "application/font")

IMAGE_EXTENSION_BY_TYPE = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/gif": ".gif", "image/avif": ".avif",
    "image/svg+xml": ".svg",
}
VIDEO_EXTENSION_BY_TYPE = {
    "video/mp4": ".mp4", "video/webm": ".webm", "video/ogg": ".ogv",
    "video/quicktime": ".mov",
    "application/vnd.apple.mpegurl": ".m3u8",
    "application/x-mpegurl": ".m3u8",
}

# Attributes that can carry a media URL. Lazy-loading themes stash the
# real URL in a data-* attribute and leave src as a 1x1 placeholder, so
# the data-* variants are checked FIRST.
MEDIA_URL_ATTRIBUTES = ("data-src", "data-original", "data-lazy-src", "data-url", "src")


def normalize_url(url: str) -> str:
    """Canonical form used to match a referenced URL against WARC target URIs.

    Drops the fragment, drops a trailing slash, lowercases the host.
    Deliberately does NOT strip the query string: for a CDN image the
    query often selects the actual variant, so two URLs differing only in
    query are genuinely different images.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, parts.query, ""))


def slugify(value: str, limit: int = 80) -> str:
    """URL slug -> safe identifier. Folds Hungarian accents so the result
    survives any filesystem and any downstream consumer."""
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value[:limit].rstrip("-") or "article"


def best_from_srcset(srcset: str) -> str | None:
    """Pick the highest-resolution candidate out of a srcset attribute.

    ``'a.jpg 400w, b.jpg 800w'`` -> ``'b.jpg'``. Falls back to the last
    entry when there are no width/density descriptors to compare.
    """
    best_url, best_weight = None, -1.0
    for candidate in srcset.split(","):
        parts = candidate.strip().split()
        if not parts:
            continue
        url = parts[0]
        weight = 0.0
        if len(parts) > 1:
            descriptor = parts[1].strip()
            try:
                weight = float(descriptor[:-1]) if descriptor[-1] in "wx" else 0.0
            except ValueError:
                weight = 0.0
        if weight >= best_weight:
            best_url, best_weight = url, weight
    return best_url


def extension_for(content_type: str, url: str, is_video: bool) -> str:
    """Choose a file extension from the response's own Content-Type, with
    the URL path as a fallback.

    Trusting the header first matters here: ripost.hu's CDN serves webp
    bytes from extensionless URLs, so going by the path would file them
    as ``.bin``.
    """
    content_type = (content_type or "").split(";")[0].strip().lower()
    table = VIDEO_EXTENSION_BY_TYPE if is_video else IMAGE_EXTENSION_BY_TYPE
    if content_type in table:
        return table[content_type]
    guessed = mimetypes.guess_extension(content_type) if content_type else None
    if guessed:
        return ".jpg" if guessed == ".jpe" else guessed
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix and len(suffix) <= 6:
        return suffix
    return ".mp4" if is_video else ".bin"


def is_http_url(url: str) -> bool:
    """True for the only schemes we will ever put in an href/src."""
    try:
        return urlsplit(url).scheme in ("http", "https")
    except ValueError:
        return False


def host_of(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower()
    except ValueError:
        return ""
