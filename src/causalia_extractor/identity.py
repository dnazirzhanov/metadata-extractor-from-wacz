"""
identity.py
===========
Where an article lives, and what it is called.

The corpus layout is::

    <PAGES_ROOT>/<outlet>/<url_hash[:2]>/<url_hash>/page.wacz

where ``url_hash = sha256(normalize_url(article URL))``. That value is the
identity of an article across the whole system - it is ``urls.url_hash`` in
Postgres, the directory name on disk, and what this extractor emits as
``archive_id``. No second identifier is invented.

TWO WAYS TO LEARN THE IDENTITY, IN THIS ORDER
---------------------------------------------
1. From the path, when the .wacz sits in the corpus layout. Free, and it is
   what the archiver actually recorded.
2. By re-deriving it from the URL Browsertrix stored in ``pages.jsonl``. This is
   what makes the extractor work on a .wacz anywhere on disk - a pilot capture in
   a Downloads folder, a one-off in /tmp - rather than only inside the corpus.

Path (1) and derivation (2) agree: verified against the live corpus on
ripost.hu ``00003bdc...`` and magyarnemzet.hu ``58b97ac5...``, both reproduced
exactly from their page URLs.

``normalize_url`` here is a faithful port of ``causalia/urltools.py`` on the
archiver side. It MUST stay byte-compatible with it or the archive_id this
extractor emits will not join to the rows Postgres already holds. It is
deliberately NOT the ``urls.normalize_url`` used for matching WARC targets -
that one keeps tracking parameters, because for a CDN image the query selects
the actual variant.

``outlet_of`` is a small registrable-domain heuristic rather than ``tldextract``:
the archiver uses tldextract, but adding it here would mean a dependency solely
to strip a subdomain from twelve known Hungarian hosts. The path is preferred
whenever it is available, so this only runs for out-of-corpus archives.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

WACZ_NAME = "page.wacz"

#: Root of the corpus. Configurable, never hardcoded to one machine's layout.
PAGES_ROOT = Path(os.environ.get(
    "CAUSALIA_PAGES_ROOT", "/mnt/hdd/c0cshf/causalia/pages"))

#: Query parameters that never identify content. Must match
#: ``causalia/urltools.py:TRACKING_PARAMS`` exactly.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "yclid", "mc_cid", "mc_eid", "igshid", "ref", "source",
    "_ga", "gclsrc", "dclid", "msclkid", "twclid",
}

#: Multi-part public suffixes that appear in this corpus. Anything else is
#: treated as a single-label suffix, which is correct for .hu and .com.
_MULTIPART_SUFFIXES = ("co.uk", "org.uk", "com.au", "co.jp", "com.br")


def canonical_url_for_identity(url: str) -> str:
    """The archiver's canonical form, used only to compute ``archive_id``.

    Lowercase scheme and host, drop default ports, drop tracking parameters,
    sort the rest, drop the fragment, strip a trailing slash on non-root paths.
    """
    if not url:
        return url
    parts = urlsplit(url.strip())

    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS]
    kept.sort()

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    return urlunsplit((scheme, netloc, path, urlencode(kept), ""))


def archive_id_for(url: str) -> str:
    """``sha256`` of the canonical URL - the system-wide identity of an article."""
    return hashlib.sha256(canonical_url_for_identity(url).encode("utf-8")).hexdigest()


def outlet_of(url: str) -> str:
    """The registrable domain: ``www.origo.hu`` and ``sport.origo.hu`` -> ``origo.hu``."""
    host = urlsplit(url).netloc.lower().split(":")[0]
    if not host:
        return ""
    for suffix in _MULTIPART_SUFFIXES:
        if host.endswith("." + suffix):
            labels = host.split(".")
            return ".".join(labels[-3:])
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def _looks_like_url_hash(name: str) -> bool:
    return len(name) == 64 and all(c in "0123456789abcdef" for c in name)


@dataclass(frozen=True)
class ArticleLocation:
    """One archived article: where its .wacz is, and what it is called."""

    wacz_path: Path
    archive_id: str | None = None
    outlet: str | None = None
    #: True when identity came from the corpus path rather than the page URL.
    from_path: bool = False

    @classmethod
    def from_wacz(cls, wacz_path) -> "ArticleLocation":
        """Identity from the corpus path, when the path has it. No file is read."""
        wacz_path = Path(wacz_path).resolve()
        directory = wacz_path.parent
        if _looks_like_url_hash(directory.name):
            # <outlet>/<h2>/<sha256> - the outlet is two levels up, and only
            # trustworthy when the hash level is where we expect it.
            return cls(wacz_path=wacz_path, archive_id=directory.name,
                       outlet=directory.parent.parent.name, from_path=True)
        return cls(wacz_path=wacz_path)

    def resolved(self, page_url: str | None) -> "ArticleLocation":
        """Fill in identity from the captured page URL when the path lacked it."""
        if self.from_path or not page_url:
            return self
        return ArticleLocation(
            wacz_path=self.wacz_path,
            archive_id=archive_id_for(page_url),
            outlet=outlet_of(page_url),
            from_path=False,
        )

    @property
    def shard(self) -> str:
        return (self.archive_id or "")[:2]

    def output_dir(self, output_root: Path) -> Path:
        """``<output>/<outlet>/<h2>/<archive_id>``, the corpus layout preserved.

        Falls back to the .wacz's own stem when identity could not be
        established at all, so the extractor still produces something usable
        rather than refusing to run.
        """
        if self.archive_id and self.outlet:
            return Path(output_root) / self.outlet / self.shard / self.archive_id
        if self.archive_id:
            return Path(output_root) / self.shard / self.archive_id
        return Path(output_root) / self.wacz_path.stem


def iter_wacz_files(root: Path, outlet: str | None = None):
    """Yield every ``.wacz`` under ``root``, deterministically ordered.

    ``root`` may be a single .wacz, a directory holding one or more of them, an
    article directory, a shard, an outlet, or a whole PAGES_ROOT. Any ``.wacz``
    name is accepted, not only ``page.wacz`` - a pilot capture in a Downloads
    folder is a legitimate input, and identity is recovered from the page URL
    when the path cannot supply it.

    ``os.scandir`` is used at each level rather than ``rglob`` because a
    recursive glob over ripost.hu stats 285,606 directories and takes minutes on
    the archive disk.
    """
    root = Path(root)
    if root.is_file():
        if root.suffix == ".wacz":
            yield root
        return
    if not root.is_dir():
        return

    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return

    archives = [Path(e.path) for e in entries
                if e.is_file() and e.name.endswith(".wacz")]
    if archives:
        # A leaf holding archives. The corpus case is exactly one page.wacz.
        yield from archives
        return

    outlet_level = _is_outlet_level(root) if outlet else False
    for entry in entries:
        if not entry.is_dir():
            continue
        if outlet_level and entry.name != outlet:
            continue
        yield from iter_wacz_files(Path(entry.path), outlet=outlet)


def _is_outlet_level(path: Path) -> bool:
    """True when ``path``'s children look like outlet directories."""
    try:
        return any("." in entry.name and entry.is_dir()
                   for entry in os.scandir(path))
    except OSError:
        return False
