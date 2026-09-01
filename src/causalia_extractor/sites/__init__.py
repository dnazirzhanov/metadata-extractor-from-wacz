# Ported from causalia-final/extractor/sites/__init__.py
"""
extractor/sites/__init__.py
===========================
Registry mapping an outlet to its extraction rules.

The architectural rule this enforces: **generic extraction logic +
site-specific rules**, never publisher selectors sprinkled through the
core. ``extractor/core`` receives a ``SiteRules`` object and asks it
questions; it never imports ``ripost`` and never branches on a hostname.

Adding an outlet is one file and one registry line. An outlet with no
entry silently gets ``SiteRules()``, so the other five outlets in this
corpus can be tried the day their crawls finish, without code.
"""

from __future__ import annotations

from .base import SiteRules
from .ripost import RipostRules

#: outlet hostname -> rules instance. Instantiated once; rules are
#: stateless and safe to share across threads.
_REGISTRY: dict[str, SiteRules] = {
    "ripost.hu": RipostRules(),
}

_DEFAULT = SiteRules()


def rules_for(outlet: str | None) -> SiteRules:
    """Rules for an outlet, falling back to the generic set.

    Tolerates a leading ``www.`` and any case, since the outlet can come
    from a directory name or from a URL's host.
    """
    if not outlet:
        return _DEFAULT
    key = outlet.strip().lower()
    if key.startswith("www."):
        key = key[4:]
    return _REGISTRY.get(key, _DEFAULT)


__all__ = ["SiteRules", "rules_for"]
