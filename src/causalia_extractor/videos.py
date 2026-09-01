"""
videos.py
=========
videos.json and the local videos/ directory.

Ported from ``causalia-final/extractor/core/videos.py``. The platform tables in
here are the expensive part - they were measured against 302 video-bearing
captures, not guessed:

* ``_PLATFORM_PATTERNS`` - videa/videakid together are the most common video
  host in this corpus (109 of 302), and videakid must be tested BEFORE videa.
* ``_CDN_PLATFORM`` - an embed record points at the PLAYER page, so its media
  sits on a CDN host no record references. Working backwards from the host is
  what took localisation from 21.5 MB to 102.3 MB on the eleven-platform test
  set.
* ``_MARKUP_EMBEDS`` - TikTok, X, Instagram and Telegram ship a <blockquote> or
  <script> their own JS hydrates, so a capture of the served HTML holds no
  iframe at all.
* ``_NON_MEDIA_EMBED_RE`` - a narrow not-media blocklist (tag managers, consent
  frames, analytics), deliberately NOT a video whitelist. Losing unknown
  platforms is the thing to avoid.

THE CORROBORATION RULE, which must not be broken: inside the article body every
embed is recorded whatever the platform. Outside the body that would sweep up
page chrome, so a candidate there is admitted only with evidence - a recognised
platform, or video bytes actually held for that exact URL.

An HLS ``.ts`` segment set must never be reported as complete: the write is
refused anyway (no ffmpeg), but a wrong flag would make a later backfill skip
exactly the videos it most needs to fetch.

CHANGED FROM THE PORT: ``position``, ``discovered_via``, ``capture_urls``,
``capture_bytes`` and ``capture_complete`` are no longer serialised. They are
extraction diagnostics, not article data. The detection machinery that used them
is unchanged and they remain available in-process for logging; a video's place
in the article is expressed once, by its content block's xpath.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from .urls import extension_for, normalize_url
from .wacz import ArchiveContents

#: host substring -> platform name. Checked in order.
_PLATFORM_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(^|\.)youtube(-nocookie)?\.com$"), "youtube"),
    (re.compile(r"(^|\.)youtu\.be$"), "youtube"),
    (re.compile(r"(^|\.)vimeo\.com$"), "vimeo"),
    (re.compile(r"(^|\.)dailymotion\.com$"), "dailymotion"),
    (re.compile(r"(^|\.)facebook\.com$"), "facebook"),
    (re.compile(r"(^|\.)instagram\.com$"), "instagram"),
    (re.compile(r"(^|\.)tiktok\.com$"), "tiktok"),
    (re.compile(r"(^|\.)(twitter|x)\.com$"), "twitter"),
    (re.compile(r"(^|\.)indavideo\.hu$"), "indavideo"),
    (re.compile(r"(^|\.)t\.me$"), "telegram"),
    (re.compile(r"(^|\.)telegram\.org$"), "telegram"),
    # videakid is checked BEFORE videa: same operator, same player markup,
    # and keeping them adjacent stops one being fixed without the other.
    # Together they are the most common video host in this corpus - 109 of
    # the 302 video-bearing captures in the 2026-08-24 survey.
    (re.compile(r"(^|\.)videakid\.hu$"), "videakid"),
    (re.compile(r"(^|\.)videa\.hu$"), "videa"),
]


#: Localisation policy. Left configurable because the corpus-wide cap is
#: a decision to take from measured sizes, not to guess now.
LOCALISE_VIDEO = os.environ.get("CAUSALIA_LOCALISE_VIDEO", "1").lower() \
    not in ("0", "false", "no")
MAX_VIDEO_BYTES = int(os.environ.get("CAUSALIA_MAX_VIDEO_BYTES", 64 * 1024 * 1024))
#: Below this a "video" payload is an error page or a tracking pixel.
MIN_VIDEO_BYTES = int(os.environ.get("CAUSALIA_MIN_VIDEO_BYTES", 4096))

#: Containers we can write as-is. HLS is deliberately absent: reassembling
#: .ts segments into a playable file needs ffmpeg, which this image does
#: not have and should not grow - it would make an offline parser depend
#: on a media toolchain.
LOCALISABLE_EXTENSIONS = frozenset({".mp4", ".webm", ".ogv", ".mov", ".m4v"})


#: Infrastructure that is embedded as an iframe but is never article
#: media: tag managers, captchas, ad tech, social SDK frames. This is NOT
#: a video whitelist - an unknown VIDEO platform is still recorded, by
#: design. It is a narrow blocklist of things we can positively say are
#: not media, and it exists because Readability does not reliably drop
#: them: a tag-manager frame surviving into the article body was being
#: recorded as a video (caught by test_document_scan_ignores_page_chrome).
_NON_MEDIA_EMBED_RE = re.compile(
    r"googletagmanager\.com"
    r"|/ns\.html\?id=GTM-"
    r"|google\.com/recaptcha|gstatic\.com/recaptcha"
    r"|doubleclick\.net|googlesyndication\.com|google-analytics\.com"
    r"|connect\.facebook\.net"
    r"|facebook\.com/plugins/(?:like|share|comments)"
    r"|onetrust\.com|cookielaw\.org|hotjar\.com",
    re.I,
)


def is_non_media_embed(url: str) -> bool:
    """True for infrastructure frames that can never be article media."""
    return bool(_NON_MEDIA_EMBED_RE.search(url or ""))


#: CDN hosts that serve a platform's actual media bytes. The embed URL
#: is the PLAYER page (youtube.com/embed/..., tiktok.com/embed/v2/...),
#: so the bytes always live at a different host and no record points at
#: them. Without this mapping the capture can hold 30 MB of mp4 for an
#: article and nothing in videos.json says so.
_CDN_PLATFORM: list[tuple[re.Pattern, str]] = [
    (re.compile(r"fbcdn\.net$|^video[\w.-]*\.xx\.fbcdn\.net$"), "facebook"),
    (re.compile(r"(^|\.)video\.twimg\.com$"), "twitter"),
    (re.compile(r"tiktokcdn"), "tiktok"),
    (re.compile(r"(^|\.)telesco\.pe$"), "telegram"),
    (re.compile(r"(^|\.)googlevideo\.com$"), "youtube"),
    (re.compile(r"(^|\.)cdninstagram\.com$"), "instagram"),
    (re.compile(r"indavideo\.hu$"), "indavideo"),
    (re.compile(r"videakid\.hu$"), "videakid"),
    (re.compile(r"videa\.hu$"), "videa"),
    (re.compile(r"(^|\.)tv2\.hu$"), "tv2play"),
    (re.compile(r"(^|\.)vimeocdn\.com$"), "vimeo"),
]


def cdn_platform(url: str) -> str | None:
    """The platform whose media a CDN host serves, if we know it."""
    host = urlsplit(url or "").netloc.lower().split(":")[0]
    for pattern, name in _CDN_PLATFORM:
        if pattern.search(host):
            return name
    return None


# ---------------------------------------------------------------------
# Reading a CDN payload's own metadata
# ---------------------------------------------------------------------
# Working backwards from the CDN HOST is enough when a page has one embed on
# that platform. With three, it is not - and the answer was in the URL all
# along. Facebook's media URLs carry a base64 ``efg`` parameter holding the
# video_id, the encode tag and the bitrate:
#
#   {"vencode_tag": "dash_r2av1-r1gen2vp9-m3_q80",
#    "video_id": 1699575194535875, "duration_s": 65, "bitrate": 1659271}
#
# Measured on mandiner.hu f300764f: three facebook reels, 19 captured payloads.
# Nine of them (q20..q90 plus a separate heaac audio track) carry video_id
# 1699575194535875, nine more carry 678407308664933, and one progressive stream
# carries none. Without this, all 19 became their own VideoRecord: videos.json
# claimed 22 videos where the article has 3, and 92 MB was written as 19 files
# none of which is playable, because a DASH video track has no audio and the
# audio is a separate file.

def _decode_efg(url: str) -> dict:
    """The decoded ``efg`` parameter of a Facebook media URL, or {}."""
    try:
        raw = parse_qs(urlsplit(url or "").query).get("efg", [""])[0]
        if not raw:
            return {}
        raw = unquote(raw)
        blob = base64.b64decode(raw + "=" * (-len(raw) % 4))
        decoded = json.loads(blob)
        return decoded if isinstance(decoded, dict) else {}
    except (ValueError, TypeError, json.JSONDecodeError, binascii.Error):
        return {}


def payload_identity(url: str) -> tuple[str | None, str | None]:
    """``(external_id, encode_tag)`` a CDN payload declares about itself.

    Both are None for a CDN that tells us nothing, which is the common case and
    is why the host fallback still exists.
    """
    meta = _decode_efg(url)
    if not meta:
        return None, None
    video_id = meta.get("video_id")
    tag = meta.get("vencode_tag")
    return (str(video_id) if video_id else None,
            str(tag) if isinstance(tag, str) else None)


def is_adaptive_rendition(tag: str | None) -> bool:
    """True for one rung of a DASH/HLS bitrate ladder.

    Not playable on its own: the video rungs carry no audio and the audio track
    is a separate file, so writing one produces a silent clip and writing all of
    them produces the same video nine times. Reassembly needs ffmpeg, which this
    extractor deliberately does not have.
    """
    return bool(tag) and tag.lower().startswith(("dash", "hls"))


def is_progressive(tag: str | None) -> bool:
    """True for a single self-contained stream - video and audio in one file."""
    return bool(tag) and "progressive" in tag.lower()


#: Embeds that are NOT an iframe. TikTok, X, Instagram and Telegram all
#: ship a <blockquote> or a <script> that their own JS hydrates into a
#: player at runtime, so a capture of the SERVED html contains no iframe
#: at all - this markup is the only trace the article ever had a video.
#: Measured 2026-08-24: this is what the remaining four misses were.
#:
#: Each entry is (tag, required class, how to get the URL). "nested-a"
#: means the id lives in a child <a href>, because the blockquote itself
#: carries no identifier - that is genuinely how X ships an embed.
_MARKUP_EMBEDS: list[tuple[str, str | None, str, str]] = [
    ("blockquote", "tiktok-embed",    "attr:cite",                  "tiktok"),
    ("blockquote", "instagram-media", "attr:data-instgrm-permalink", "instagram"),
    ("blockquote", "twitter-tweet",   "nested-a:/status/",          "twitter"),
    ("div",        "fb-video",        "attr:data-href",             "facebook"),
    ("div",        "fb-post",         "attr:data-href",             "facebook"),
    # The widget script is included site-wide on magyarnemzet with NO
    # data-telegram-post; requiring the attribute is what stops every
    # article being credited with a phantom Telegram video.
    ("script",     None,              "attr:data-telegram-post",    "telegram"),
]

#: data-telegram-post is "channel/123", not a URL.
_ATTR_IS_PATH = {"data-telegram-post": "https://t.me/{}"}


def markup_embed_url(element: Tag) -> str | None:
    """The embed URL a non-iframe player element points at, or None."""
    classes = " ".join(element.get("class") or [])
    for tag, required, how, _platform in _MARKUP_EMBEDS:
        if element.name != tag:
            continue
        if required and required not in classes.split():
            continue

        if how.startswith("attr:"):
            name = how[len("attr:"):]
            raw = str(element.get(name) or "").strip()
            if not raw:
                continue
            template = _ATTR_IS_PATH.get(name)
            return template.format(raw) if template else raw

        if how.startswith("nested-a:"):
            needle = how[len("nested-a:"):]
            for anchor in element.find_all("a", href=True):
                if needle in anchor["href"]:
                    return anchor["href"].strip()
    return None


@dataclass
class VideoRecord:
    id: str
    type: str                       # platform name, or 'html5'
    platform: str | None = None     # matches videos.platform in Postgres
    external_id: str | None = None  # matches videos.external_id
    url: str | None = None          # canonical watch URL where known
    embed_url: str | None = None    # the URL actually embedded in the page
    thumbnail_url: str | None = None
    title: str | None = None
    caption: str | None = None
    position: int | None = None
    #: Which pass found it: "reader" (the article body), "document" (on
    #: the page but outside the body). A list because one video can be
    #: found by more than one, and the union is the honest answer.
    discovered_via: list[str] = field(default_factory=list)
    #: Bytes held for this video in the capture, and whether they are a
    #: whole file. A 206 range or an HLS segment set is evidence, but it
    #: is not something you can play.
    #: Payload URLs in the capture attributed to this video. An embed's
    #: bytes live on a CDN, never at the embed URL itself.
    capture_urls: list[str] = field(default_factory=list)
    capture_bytes: int | None = None
    capture_complete: bool | None = None
    archived: bool = False          # are the bytes in our WACZ?
    local_file: str | None = None

    def to_dict(self) -> dict:
        """The persisted shape. position / discovered_via / capture_* are
        extraction diagnostics and stay in-process; a video's place in the
        article is expressed once, by its content block's xpath."""
        return {
            "id": self.id,
            "type": self.type,
            "platform": self.platform,
            "external_id": self.external_id,
            "url": self.url,
            "embed_url": self.embed_url,
            "thumbnail_url": self.thumbnail_url,
            "title": self.title,
            "caption": self.caption,
            "local_file": self.local_file,
            "archived": self.archived,
        }


def identify_platform(url: str) -> str | None:
    host = urlsplit(url).netloc.lower().split(":")[0]
    for pattern, name in _PLATFORM_PATTERNS:
        if pattern.search(host):
            return name
    return None


def parse_embed(url: str) -> tuple[str | None, str | None, str | None, str | None]:
    """(platform, external_id, canonical_url, thumbnail_url) for an embed URL.

    Thumbnail URLs are RECORDED, never fetched - they are a pointer for a
    future consumer that has decided to go online, not something this
    extractor resolves.
    """
    platform = identify_platform(url)
    if platform is None:
        return None, None, None, None

    parts = urlsplit(url)
    path = parts.path
    external_id = None

    if platform == "youtube":
        if "youtu.be" in parts.netloc:
            external_id = path.strip("/").split("/")[0] or None
        elif path.startswith("/embed/"):
            external_id = path[len("/embed/"):].split("/")[0] or None
        elif path.startswith("/shorts/"):
            external_id = path[len("/shorts/"):].split("/")[0] or None
        else:
            external_id = (parse_qs(parts.query).get("v") or [None])[0]
        if external_id:
            return (platform, external_id,
                    f"https://www.youtube.com/watch?v={external_id}",
                    f"https://i.ytimg.com/vi/{external_id}/hqdefault.jpg")

    elif platform == "vimeo":
        match = re.search(r"/(?:video/)?(\d+)", path)
        if match:
            external_id = match.group(1)
            return (platform, external_id,
                    f"https://vimeo.com/{external_id}", None)

    elif platform == "tiktok":
        # /@user/video/<id> (the blockquote's cite) or /embed/v2/<id>.
        # No canonical URL: rebuilding one needs the @user handle, which
        # /embed/v2/ does not carry.
        match = re.search(r"/(?:video|embed/v2)/(\d+)", path)
        if match:
            external_id = match.group(1)

    elif platform == "instagram":
        match = re.search(r"/(?:p|reel|tv)/([\w-]+)", path)
        if match:
            external_id = match.group(1)
            return (platform, external_id,
                    f"https://www.instagram.com/p/{external_id}/", None)

    elif platform == "twitter":
        # The tweet id lives in a nested <a href=".../status/<id>">; the
        # blockquote itself carries no identifier at all.
        match = re.search(r"/status/(\d+)", path)
        if match:
            external_id = match.group(1)
            return (platform, external_id,
                    f"https://twitter.com/i/status/{external_id}", None)

    elif platform == "telegram":
        # data-telegram-post="channel/123" -> t.me/channel/123. The id is
        # the channel-qualified pair; a bare post number is not unique.
        match = re.search(r"^/(\w+/\d+)", path)
        if match:
            external_id = match.group(1)
            return (platform, external_id,
                    f"https://t.me/{external_id}", None)

    elif platform in ("videa", "videakid"):
        # Both embed as <host>/player?v=<opaque token>. There is no watch
        # URL derivable from that token alone - videa's public page is
        # keyed by a numeric fid and a slug, neither of which is in the
        # embed - so the canonical URL stays None rather than fabricated.
        external_id = (parse_qs(parts.query).get("v") or [None])[0]

    elif platform == "facebook":
        external_id = (parse_qs(parts.query).get("href") or [None])[0]

    return platform, external_id, None, None


class VideoExtractor:
    """Finds every video in the reader body and stamps it with an id."""

    def __init__(self, contents: ArchiveContents, base_url: str, writer=None):
        self.contents = contents
        self.base_url = base_url
        #: None in a caller that only wants detection (the surveys). The
        #: pipeline always passes one.
        self.writer = writer
        self.notes: list[str] = []
        self.records: list[VideoRecord] = []
        self.warnings: list[str] = []
        self.markup_adopted = 0
        self._count = 0

    def _next_id(self) -> str:
        self._count += 1
        return f"video_{self._count:03d}"

    def _caption_for(self, element: Tag) -> str | None:
        figure = element.find_parent("figure")
        if figure is not None:
            figcaption = figure.find("figcaption")
            if figcaption is not None:
                return " ".join(figcaption.get_text(" ", strip=True).split()) or None
        return None

    def _adopt_markup_embeds(self, soup: BeautifulSoup) -> int:
        """Turn non-iframe player markup into the canonical embed div.

        Runs FIRST, so everything downstream - the data-embed-url loop
        below, blocks.py, render.decorate_embeds, sanitize - sees one
        shape and needs no per-platform knowledge.

        TikTok's blockquote carries its own ``data-video-id``, which is
        the exact attribute this class stamps on its elements. The URL is
        therefore read off the ORIGINAL element and a fresh <div> is
        built, so a platform's value can never be mistaken for one of
        ours.
        """
        adopted = 0
        for tag, _required, _how, _platform in _MARKUP_EMBEDS:
            for element in list(soup.find_all(tag)):
                if element.find_parent(attrs={"data-embed-url": True}):
                    continue                    # already inside an embed
                raw = markup_embed_url(element)
                if not raw:
                    continue
                absolute = urljoin(self.base_url, raw)
                placeholder = soup.new_tag("div")
                placeholder["class"] = "embed"
                placeholder["data-embed-url"] = absolute
                element.replace_with(placeholder)
                adopted += 1
        return adopted

    def process(self, soup: BeautifulSoup) -> None:
        # 0. non-iframe players first, so the loop below sees them too.
        self.markup_adopted = self._adopt_markup_embeds(soup)

        # 1. third-party players, left as <div data-embed-url> by
        #    readability.restore_embeds
        for element in list(soup.find_all(attrs={"data-embed-url": True})):
            raw = str(element.get("data-embed-url") or "").strip()
            if not raw:
                continue
            absolute = urljoin(self.base_url, raw)
            if is_non_media_embed(absolute):
                # Not media, so it is not a video AND it has no business
                # rendering as an embed card in the reader view.
                element.decompose()
                continue
            platform, external_id, canonical, thumbnail = parse_embed(absolute)
            identifier = self._next_id()
            element["data-video-id"] = identifier
            if platform:
                element["data-embed-platform"] = platform
            self.records.append(VideoRecord(
                id=identifier,
                type=platform or "iframe",
                platform=platform,
                external_id=external_id,
                url=canonical,
                embed_url=absolute,
                thumbnail_url=thumbnail,
                caption=self._caption_for(element),
                archived=False,
                discovered_via=["reader"],
            ))

        # 2. native <video>. Unlike an iframe these bytes CAN be in the
        #    capture, so check before declaring them missing.
        for element in soup.find_all("video"):
            source = element.get("src") or ""
            if not source:
                child = element.find("source")
                source = (child.get("src") if child else "") or ""
            absolute = urljoin(self.base_url, source.strip()) if source.strip() else None
            identifier = self._next_id()
            element["data-video-id"] = identifier

            record = VideoRecord(
                id=identifier,
                type="html5",
                platform="html5",
                external_id=None,
                url=absolute,
                embed_url=absolute,
                caption=self._caption_for(element),
                archived=False,
                discovered_via=["reader"],
            )
            self.records.append(record)
            # Sets archived/capture_* and, when the bytes are usable,
            # writes videos/<id>.<ext> and points the element at it.
            self._localise(record, element)
            if not record.archived and absolute:
                self.warnings.append(f"video not captured in archive: {absolute}")

    def _payload_for(self, url: str | None):
        if not url:
            return None
        payload = self.contents.payloads.get(normalize_url(url))
        if payload is None or not payload.content_type.startswith("video/"):
            return None
        return payload

    @staticmethod
    def _range_is_whole_file(content_range: str | None, size: int) -> bool | None:
        """Is a 206 body the entire file? None when it cannot be told.

        ``Content-Range: bytes 0-99/100`` is complete; ``bytes 0-49/100``
        is a playable prefix; ``bytes 50-99/100`` is not a container at
        all and must never be written.
        """
        if not content_range:
            return None
        match = re.search(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range)
        if not match:
            return None
        start, end, total = match.group(1), match.group(2), match.group(3)
        if int(start) != 0:
            return False
        if total == "*":
            return None
        return int(end) + 1 >= int(total)

    def _best_payload(self, record: VideoRecord):
        """``(payload, encode_tag)`` best representing this record's bytes.

        A self-contained stream always beats a rung of a bitrate ladder, however
        much smaller it is. Picking purely by size selects the q90 DASH video
        track - the biggest file and a silent one.
        """
        candidates = []
        for url in list(record.capture_urls) + [record.embed_url, record.url]:
            payload = self._payload_for(url)
            if payload is not None:
                _, tag = payload_identity(url)
                candidates.append((payload, tag))
        if not candidates:
            return None, None
        return max(candidates,
                   key=lambda c: (not is_adaptive_rendition(c[1]), len(c[0].body)))

    def _localise(self, record: VideoRecord, element=None) -> None:
        """Write the captured bytes into videos/, if we may.

        Sets ``local_file`` and, when an element is given, points it at
        the local path so the reader view can actually play it. Every
        refusal is a NOTE, not a warning: a size cap is a policy choice
        and marking the article "partial" for it would make that status
        meaningless.
        """
        if record.local_file:
            return
        payload, encode_tag = self._best_payload(record)
        if payload is None:
            return

        size = len(payload.body)
        record.capture_bytes = size
        record.archived = True

        whole = True
        if payload.status.startswith("206"):
            verdict = self._range_is_whole_file(payload.content_range, size)
            if verdict is False:
                record.capture_complete = False
                self.notes.append(
                    f"video bytes are a partial range that does not start at 0, "
                    f"not written: {record.embed_url}")
                return
            whole = bool(verdict)
        record.capture_complete = whole

        if is_adaptive_rendition(encode_tag):
            # Every rung this record holds is part of a DASH/HLS ladder. The
            # bytes are real and `archived` says so, but none of them is a file
            # you can play: the video rungs are silent and the audio track is
            # separate. Reassembly needs ffmpeg, which this extractor does not
            # have - so nothing is written and capture_complete stays False, or
            # the backfill stage would skip exactly the videos it must fetch.
            record.capture_complete = False
            self.notes.append(
                f"video is an adaptive stream ({encode_tag}); its rungs cannot "
                f"be written as one file: {record.embed_url}")
            return

        if not LOCALISE_VIDEO or self.writer is None:
            return
        if size < MIN_VIDEO_BYTES:
            self.notes.append(f"video payload too small to be real ({size} B): "
                              f"{record.embed_url}")
            return
        if size > MAX_VIDEO_BYTES:
            self.notes.append(
                f"video not localised, {size / 1048576:.1f} MB is over the "
                f"{MAX_VIDEO_BYTES / 1048576:.0f} MB cap: {record.embed_url}")
            return

        extension = extension_for(payload.content_type,
                                  record.embed_url or "", is_video=True)
        if extension not in LOCALISABLE_EXTENSIONS:
            # A set of .ts segments is not a complete file, whatever the
            # individual responses said. Leaving this True would tell the
            # backfill stage the video is already in hand and make it skip
            # exactly the videos it most needs to fetch.
            record.capture_complete = False
            self.notes.append(
                f"video container {extension} cannot be written as a file "
                f"(HLS/DASH needs reassembly): {record.embed_url}")
            return

        self.writer.write_bytes(f"videos/{record.id}{extension}", payload.body)
        record.local_file = f"videos/{record.id}{extension}"
        if element is not None:
            element["src"] = record.local_file
            element["preload"] = "metadata"
            element.attrs.pop("data-archive-missing", None)

    def _identity(self, platform: str | None, external_id: str | None,
                  url: str | None) -> str:
        """The key two sightings of the same video must agree on."""
        if platform and external_id:
            return f"{platform}:{external_id}"
        return normalize_url(url or "")

    def _document_candidates(self, soup: BeautifulSoup):
        """Every URL on the page that could be a player, raw."""
        for frame in soup.find_all("iframe"):
            raw = (frame.get("data-src") or frame.get("src") or "").strip()
            if raw and not raw.startswith("data:"):
                yield raw
        for tag, _required, _how, _platform in _MARKUP_EMBEDS:
            for element in soup.find_all(tag):
                raw = markup_embed_url(element)
                if raw:
                    yield raw
        for video in soup.find_all("video"):
            raw = (video.get("src") or "").strip()
            if not raw:
                source = video.find("source")
                raw = ((source.get("src") or source.get("data-src"))
                       if source else "") or ""
            raw = raw.strip()
            if raw and not raw.startswith("data:"):
                yield raw

    def scan_document(self, soup: BeautifulSoup) -> int:
        """Record players that are on the PAGE but not in the article body.

        Readability drops a player whose markup is nearly all links and
        no prose - which is exactly what a TikTok blockquote is - so the
        embed never reaches the reader body even though the page plainly
        had one. Measured 2026-08-24: this was the last of the eleven
        video types still missed.

        Read-only: it never mutates the document, because these embeds
        are not rendered into the reader view. They are recorded with
        ``position: None``, since they have no place in the prose, and
        ``discovered_via: ["document"]`` so a consumer can tell an
        article's own video from one that was merely on the page.

        THE CORROBORATION RULE. Inside the article body every embed is
        recorded whatever the platform - no whitelist. Out here that
        would sweep up page chrome: tag managers, recaptcha frames, ad
        slots. So a candidate on the rest of the page is admitted only
        with evidence - a platform we recognise, or video bytes actually
        present in this capture. An unknown platform embedded outside
        the body is therefore still missed; the WACZ-index net is what
        closes that, and it is a separate pass.
        """
        known: dict[str, VideoRecord] = {}
        for record in self.records:
            known[self._identity(record.platform, record.external_id,
                                 record.embed_url)] = record

        added = 0
        for raw in self._document_candidates(soup):
            absolute = urljoin(self.base_url, raw)
            if is_non_media_embed(absolute):
                continue
            platform, external_id, canonical, thumbnail = parse_embed(absolute)
            key = self._identity(platform, external_id, absolute)

            existing = known.get(key)
            if existing is not None:
                if "document" not in existing.discovered_via:
                    existing.discovered_via.append("document")
                continue

            archived = False
            if platform is None:
                # No recognised platform: admit it only if we are holding
                # video bytes for this exact URL.
                payload = self.contents.payloads.get(normalize_url(absolute))
                if payload is None or not payload.content_type.startswith("video/"):
                    continue
                platform, archived = "html5", True

            identifier = self._next_id()
            record = VideoRecord(
                id=identifier,
                type=platform,
                platform=platform,
                external_id=external_id,
                url=canonical,
                embed_url=absolute,
                thumbnail_url=thumbnail,
                archived=archived,
                position=None,
                discovered_via=["document"],
            )
            self.records.append(record)
            self._localise(record)
            known[key] = record
            added += 1
        return added

    def attach_payloads(self) -> int:
        """Attribute captured video bytes to the video they belong to.

        Runs after both detection passes. An embed record points at the PLAYER
        page, so its bytes - which really are in the WACZ - are never found by
        URL. This walks the payloads instead.

        Attribution is tried in order of how much it actually knows:

        1. **The payload's own metadata.** A Facebook media URL states its
           ``video_id``, so three reels on one page are told apart exactly.
        2. **The CDN host**, when the page has exactly one embed on that
           platform. This is the older rule and still covers most captures.

        Ambiguity is never guessed: a payload that neither step can place is not
        attached to a record it might not belong to.

        What happens to an unplaceable payload depends on what it IS. A
        self-contained stream gets its own record - bytes we hold must appear in
        videos.json. One rung of a DASH/HLS bitrate ladder does not: it cannot
        be played (the video rungs are silent, the audio is a separate file),
        and nine rungs of one video are not nine videos. Those bytes stay in the
        WACZ, which is the archive of record, and a note says so.
        """
        claimed: set[str] = set()
        for record in self.records:
            for url in list(record.capture_urls) + [record.embed_url, record.url]:
                if url:
                    claimed.add(normalize_url(url))

        attached = 0
        stranded_rungs = 0
        for url, payload in sorted(self.contents.payloads.items()):
            if not payload.content_type.startswith("video/"):
                continue
            if url in claimed:
                continue

            external_id, encode_tag = payload_identity(url)
            platform = cdn_platform(url)
            target = None

            # 1. the payload names its own video
            if external_id:
                for record in self.records:
                    if platform and record.platform != platform:
                        continue        # a bare id must not cross platforms
                    haystack = " ".join(filter(None, [
                        record.external_id, record.url, record.embed_url]))
                    if external_id in haystack:
                        target = record
                        break

            # 2. the CDN host, when there is only one candidate
            if target is None:
                candidates = [r for r in self.records
                              if platform and r.platform == platform]
                if len(candidates) == 1:
                    target = candidates[0]

            if target is not None:
                target.capture_urls.append(url)
                claimed.add(url)
                attached += 1
                continue

            if is_adaptive_rendition(encode_tag):
                stranded_rungs += 1
                claimed.add(url)
                continue

            identifier = self._next_id()
            record = VideoRecord(
                id=identifier,
                type=platform or "html5",
                platform=platform,
                external_id=None,
                url=None,
                embed_url=url,
                capture_urls=[url],
                archived=True,
                discovered_via=["capture"],
            )
            self.records.append(record)
            claimed.add(url)
            attached += 1

        if stranded_rungs:
            self.notes.append(
                f"{stranded_rungs} adaptive-stream rung(s) held in the capture "
                f"could not be attributed to a player on this page; not recorded "
                f"as videos")

        for record in self.records:
            if record.capture_urls:
                self._localise(record)
        return attached

    def assign_positions(self, blocks) -> None:
        by_id = {record.id: record for record in self.records}
        for block in blocks:
            if block.type == "video" and block.video_id in by_id:
                by_id[block.video_id].position = block.index


def videos_payload(records: list[VideoRecord]) -> list[dict]:
    return [record.to_dict() for record in records]
