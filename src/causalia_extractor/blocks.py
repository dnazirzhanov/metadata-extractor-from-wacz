"""
blocks.py
=========
content.json: the ordered semantic blocks of the article, each addressed by an
XPath into readability.html.

    {"type": "paragraph", "index": 1,
     "xpath": "/html/body/article/div/p[1]", "text": "..."}
    {"type": "image", "index": 2,
     "xpath": "/html/body/article/div/figure[1]/img", "image_id": "image_001"}

There is deliberately no ``block_id``. The canonical reference is
XPath -> element in readability.html, and a hash of the text is not that.

Blocks are built AFTER readability.html has been serialised and read back, so
every xpath is generated against the document that actually shipped. Each one is
validated the moment it is generated: it must select exactly one element, and
that element must be the one the path was generated for. A block whose xpath
does not validate is dropped and reported - never emitted unchecked.
"""

from __future__ import annotations

from .models import (
    BLOCK_TYPES, HEADING, IMAGE, LIST, TEXTUAL_TYPES, VIDEO, Block, ListItem)
from .normalize import normalize_text
from .xpath import XPathMismatch, xpath_for_validated


class BlockAlignmentError(Exception):
    """The serialised document does not hold the blocks that were built.

    Only reachable if HTML serialisation restructured the tree. Raising is
    correct: the alternative is emitting XPaths that describe a document nobody
    ever wrote.
    """


def build_blocks(tree, specs) -> tuple[list[Block], list[str]]:
    """Pair the built block specs with the re-parsed elements and address them."""
    from .dom import walk_blocks

    found = walk_blocks(tree)
    if len(found) != len(specs):
        raise BlockAlignmentError(
            f"readability.html holds {len(found)} blocks but {len(specs)} were "
            f"built; serialisation changed the document")

    blocks: list[Block] = []
    warnings: list[str] = []
    index = 0

    for (kind, element), spec in zip(found, specs):
        if kind != spec.type:
            raise BlockAlignmentError(
                f"block {index + 1} is a {kind} in the document but was built "
                f"as a {spec.type}")
        if kind not in BLOCK_TYPES:            # unreachable; a guard, not a branch
            continue
        try:
            xpath = xpath_for_validated(tree, element)
        except XPathMismatch as exc:
            warnings.append(f"dropped a {kind} block: {exc}")
            continue

        index += 1
        block = Block(type=kind, index=index, xpath=xpath)

        if kind == HEADING:
            block.level = spec.level
        elif kind == IMAGE:
            block.image_id = spec.image_id
        elif kind == VIDEO:
            block.video_id = spec.video_id

        if kind == LIST:
            for position, li in enumerate(element.iterchildren("li"), start=1):
                try:
                    item_xpath = xpath_for_validated(tree, li)
                except XPathMismatch as exc:
                    warnings.append(f"dropped a list item: {exc}")
                    continue
                block.items.append(ListItem(index=position, xpath=item_xpath,
                                            text=normalize_text(li)))

        if kind in TEXTUAL_TYPES:
            block.text = normalize_text(element)

        blocks.append(block)

    return blocks, warnings


def blocks_to_content(blocks) -> dict:
    return {"blocks": [block.to_dict() for block in blocks]}


def word_count(blocks) -> int:
    return sum(len(block.text.split()) for block in blocks if block.text)
