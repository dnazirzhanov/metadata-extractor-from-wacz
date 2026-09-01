"""
xpath.py
========
Absolute XPath generation against the canonical document, and the validation
that no XPath is ever emitted without.

The coordinate system is ``readability.html`` and nothing else. Paths are
generated from a re-parse of the exact bytes written to that file (see
``dom.py``), never from the in-memory tree that produced it: HTML serialisation
can move nodes - an implied ``<tbody>``, a misnested inline element - and a path
computed before serialisation can be a correct description of a tree that was
never written to disk.

FORM
----
An absolute positional path from the document root, with a ``[n]`` predicate
only where the element has more than one sibling of the same tag::

    /html/body/article/div/p[1]
    /html/body/article/div/p[2]
    /html/body/article/div/figure[1]/img

That is ``lxml``'s ``getpath()`` form, and it is also what a browser's
``document.evaluate()`` resolves. For an HTML document, browsers match
unprefixed XPath name tests against HTML elements, so the same string works on
both sides without namespace handling.

VALIDATION
----------
``validate`` re-evaluates every generated path against the tree it came from and
requires that it selects exactly one node, and that the node IS the element the
path was generated for - identity, not equality. A path that fails is never
written; its block is dropped and the extraction is downgraded to ``partial``.
"""

from __future__ import annotations

from lxml import etree


class XPathMismatch(Exception):
    """A generated XPath did not resolve back to the element it describes."""


def xpath_for(element) -> str:
    """The absolute XPath of ``element`` within its document."""
    return element.getroottree().getpath(element)


def resolve(tree, xpath: str):
    """Evaluate ``xpath`` and return the single element it selects, else None.

    Returns None for a path that selects nothing, selects more than one node, or
    is not a valid XPath expression at all. Ambiguity is a failure here, not
    something to resolve by taking the first hit.
    """
    try:
        found = tree.xpath(xpath)
    except etree.XPathError:
        return None
    if not isinstance(found, list) or len(found) != 1:
        return None
    node = found[0]
    return node if isinstance(node.tag, str) else None


def validate(tree, element, xpath: str) -> None:
    """Raise ``XPathMismatch`` unless ``xpath`` selects exactly ``element``."""
    found = resolve(tree, xpath)
    if found is None:
        raise XPathMismatch(f"xpath does not resolve to a unique element: {xpath}")
    if found is not element:
        raise XPathMismatch(
            f"xpath resolves to <{found.tag}>, not the intended "
            f"<{element.tag}>: {xpath}")


def xpath_for_validated(tree, element) -> str:
    """Generate and immediately validate. The only entry point callers should use."""
    xpath = xpath_for(element)
    validate(tree, element, xpath)
    return xpath
