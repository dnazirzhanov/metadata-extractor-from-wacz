"""
models.py
=========
The artifact data model. Plain dataclasses with an explicit ``to_dict``.

pydantic is deliberately not used - the project rejected it upstream for the
same reason it applies here: these objects are serialised once, to a documented
JSON contract, and an explicit ``to_dict`` is the contract.

Field sets are the ones the new contract calls for. Notably absent, and absent
on purpose:

* ``block_id`` anywhere. The canonical reference is XPath into readability.html.
* ``position`` on images, videos and links. A thing's place in the article is
  expressed once, by its content block's ``xpath``, and not duplicated.
* ``size_bytes``, ``discovered_via``, ``capture_urls``, ``capture_bytes``,
  ``field_sources``, ``site_name``, ``url_hash``, ``capture_title`` - extraction
  diagnostics and duplicated identity that no consumer of the article needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PARAGRAPH = "paragraph"
HEADING = "heading"
IMAGE = "image"
VIDEO = "video"
QUOTE = "quote"
LIST = "list"

#: The block types this extractor emits. paragraph/heading/image/video are the
#: required set; quote and list are included because a blockquote and a list
#: carry real article prose, and dropping them would delete text from
#: content.json and from any full-text search built on it.
BLOCK_TYPES = (PARAGRAPH, HEADING, IMAGE, VIDEO, QUOTE, LIST)

#: Types whose block carries a ``text`` field that must equal the normalised
#: text of the element its xpath selects (Invariant A).
TEXTUAL_TYPES = (PARAGRAPH, HEADING, QUOTE, LIST)


@dataclass
class ListItem:
    """One ``<li>``, addressable in its own right."""

    index: int
    xpath: str
    text: str

    def to_dict(self) -> dict:
        return {"index": self.index, "xpath": self.xpath, "text": self.text}


@dataclass
class Block:
    """One semantic content block in readability.html."""

    type: str
    index: int = 0
    xpath: str = ""
    text: str = ""
    level: int | None = None
    image_id: str | None = None
    video_id: str | None = None
    items: list[ListItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        payload: dict = {"type": self.type, "index": self.index, "xpath": self.xpath}
        if self.type == HEADING and self.level is not None:
            payload["level"] = self.level
        if self.type == IMAGE:
            payload["image_id"] = self.image_id
        elif self.type == VIDEO:
            payload["video_id"] = self.video_id
        if self.type == LIST:
            payload["items"] = [item.to_dict() for item in self.items]
        if self.type in TEXTUAL_TYPES:
            payload["text"] = self.text
        return payload


@dataclass
class ImageRecord:
    id: str
    filename: str | None = None
    original_url: str | None = None
    caption: str | None = None
    alt: str | None = None
    credit: str | None = None
    width: int | None = None
    height: int | None = None
    mime_type: str | None = None
    image_available: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "filename": self.filename,
            "original_url": self.original_url,
            "caption": self.caption,
            "alt": self.alt,
            "credit": self.credit,
            "width": self.width,
            "height": self.height,
            "mime_type": self.mime_type,
            "image_available": self.image_available,
        }


@dataclass
class VideoRecord:
    id: str
    type: str
    platform: str | None = None
    external_id: str | None = None
    url: str | None = None
    embed_url: str | None = None
    thumbnail_url: str | None = None
    title: str | None = None
    caption: str | None = None
    local_file: str | None = None
    archived: bool = False

    def to_dict(self) -> dict:
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


@dataclass
class LinkRecord:
    url: str
    text: str
    context: str | None = None
    internal: bool = False
    selector: dict | None = None

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "text": self.text,
            "context": self.context,
            "internal": self.internal,
            "selector": self.selector,
        }
