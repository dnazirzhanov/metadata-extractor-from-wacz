"""
boilerplate.py
==============
Remove the non-article "furniture" from a page BEFORE Readability sees it, and
scrub what survives into the extracted blocks.

Ported unchanged from ``causalia-final/extractor/core/boilerplate.py``. The
rules encode measured facts about this corpus: on the first real ripost.hu
capture the "article" text was dominated by a cookie-consent wall, an 18+
age-gate, and the whole site footer listing every regional paper.

Structural first (drop elements that are furniture by their HTML role or their
id/class), then textual (a backstop phrase list, in case a redesign moves the
consent copy out of a recognisable container). Neither is enough alone.

WHERE THIS RUNS, AND WHERE IT MUST NOT: the structural pass runs ONLY on the
copy handed to Readability. Metadata reads the ORIGINAL, unstripped document,
because JSON-LD, OpenGraph and the canonical link all live in <head> and inside
elements this pass is happy to delete.

Bias: it is worse to delete a paragraph of real article than to leave one line
of footer behind. When in doubt, keep the text.
"""
from __future__ import annotations

import re
import unicodedata

from bs4 import BeautifulSoup, Comment


def fold(text: str) -> str:
    """Lowercase and strip diacritics so 'Elmúltam' and 'elmultam' compare
    equal. Extractors normalise accents differently and we do not want a
    stray combining mark to let boilerplate through."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


# ---------------------------------------------------------------------
# Structural rules
# ---------------------------------------------------------------------

#: Tags that are furniture by their very role. Everything inside goes.
FURNITURE_TAGS = ["nav", "header", "footer", "aside", "form",
                  "script", "style", "noscript", "svg", "button"]

#: id/class fragments marking a container as furniture, matched
#: case-insensitively as substrings so "cookie-banner", "cookieConsent"
#: and "gdpr-cookie" all match "cookie". Kept narrow on purpose - generic
#: words like "content" or "main" could match the article itself.
FURNITURE_ID_CLASS = [
    "cookie", "consent", "gdpr", "privacy-banner", "iab",
    "age-gate", "agegate", "adult", "korhatar",          # hu: age limit
    "newsletter", "hirlevel",                            # hu: newsletter
    "related", "kapcsolodo", "ajanlo", "ajanljuk",       # hu: recommended
    "footer", "labrc", "lablec",                         # hu: footer
    "header", "fejlec",                                  # hu: header
    "nav", "menu", "navbar",
    "share", "social", "megosztas", "kovetes",           # hu: share/follow
    "sidebar", "widget",
    "comment", "hozzaszolas", "komment",                 # hu: comments
    "advert", "reklam", "hirdetes", "banner",            # hu: ad
    "subscribe", "elofizet",                             # hu: subscribe
    "breadcrumb", "morzsa",                              # hu: breadcrumb
    "tag-list", "cimke",                                 # hu: tags
    "regional", "portfolio", "regionalis",               # the baon.hu... block
]


def strip_furniture(soup: BeautifulSoup, keep_tags: frozenset[str] = frozenset()) -> BeautifulSoup:
    """Remove furniture elements from ``soup``, in place, and return it.

    ``keep_tags`` lets a site rule veto a tag from FURNITURE_TAGS - some
    layouts put the article's own hero inside a ``<header>``, and losing
    it costs the article its lead image.
    """
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    for tag_name in FURNITURE_TAGS:
        if tag_name in keep_tags:
            continue
        for element in soup.find_all(tag_name):
            element.decompose()

    for element in soup.find_all(True):
        # A decompose() earlier in THIS loop can already have detached a
        # descendant we snapshotted before it happened - decompose() sets
        # the tag's attrs to None. Skip what is already gone rather than
        # crashing on a dead reference.
        if element.attrs is None:
            continue
        identifier = " ".join(filter(None, [
            element.get("id", "") or "",
            " ".join(element.get("class", []) or []),
            element.get("role", "") or "",
        ])).lower()
        if not identifier:
            continue
        if any(marker in identifier for marker in FURNITURE_ID_CLASS):
            element.decompose()

    return soup


def strip_furniture_html(html: str, keep_tags: frozenset[str] = frozenset()) -> str:
    """``strip_furniture`` over a string of HTML.

    Uses html.parser rather than lxml here to match the original
    implementation's behaviour on the malformed markup these pages
    contain; re-serialising also normalises broken nesting on the way out.
    """
    soup = BeautifulSoup(html, "html.parser")
    return str(strip_furniture(soup, keep_tags))


# ---------------------------------------------------------------------
# Textual rules (backstop, applied to extracted block text)
# ---------------------------------------------------------------------

BOILERPLATE_PHRASES = [
    # cookie / consent
    "az ön adatainak védelme fontos",
    "sütiket és más technológiákat",
    "partnerünkkel",
    "hozzájárulását bármikor módosíthatja",
    "süti beállítások",
    "adatvédelmi tájékoztató",
    "felhasználási feltételek",
    "további lehetőségek",
    "elfogadom",
    "cookie",
    # age gate
    "kiskorúakra károsak lehetnek",
    "szűrőprogram",
    "elmúltam 18 éves",
    "nem múltam el 18 éves",
    "ha ön elmúlt 18 éves",
    # footer / organisation
    "minden jog fenntartva",
    "portfóliónk minőségi tartalmat",
    "regionális hírportálok",
    "impresszum",
    "szerzőink",
    "névnapja",                       # the "Liliána, Olga névnapja" widget
    # newsletter signup, phrased generically so it catches other sites
    # using the same template rather than hard-coding one outlet's name
    "top hírek",
    "hírlevél-feliratkozás",
    "nem akar lemaradni a",
    "adja meg a nevét és az e-mail",
    "feliratkozom a hírlevélre",
    "iratkozzon fel hírlevelünkre",
]

#: Regional paper domains that appear as a footer link list.
REGIONAL_DOMAINS = re.compile(
    r"\b(baon|bama|beol|boon|delmagyar|duol|feol|kisalfold|haon|heol|"
    r"szoljon|kemma|nool|sonline|szon|teol|vaol|veol|zaol)\.hu\b",
    re.IGNORECASE,
)

# Folded once at import so matching is accent-insensitive without
# re-folding the whole list on every line.
_FOLDED_PHRASES = [fold(p) for p in BOILERPLATE_PHRASES]


def is_boilerplate_line(line: str) -> bool:
    """True if this single line of extracted text is furniture.

    Conservative by construction: real article sentences are long and do
    not match any of these patterns.
    """
    line = line.strip()
    if not line:
        return False

    if any(phrase in fold(line) for phrase in _FOLDED_PHRASES):
        return True

    if REGIONAL_DOMAINS.search(line) and len(line) < 60:
        return True

    # A lone 1-2 word ALL CAPS line is a section label ("POLITIK",
    # "SZTÁR"), never article prose.
    words = line.split()
    if len(words) <= 2 and line.isupper() and len(line) < 20:
        return True

    return False


def clean_extracted_text(text: str) -> str:
    """Drop boilerplate lines from already-extracted article text."""
    if not text:
        return text
    kept = ["" if not line.strip() else line.strip()
            for line in text.splitlines()
            if not is_boilerplate_line(line)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def boilerplate_ratio(original: str, cleaned: str) -> float:
    """How much of the extracted text was boilerplate, as a 0-1 fraction.

    A quality signal worth keeping: a page where 80% of the "article" was
    furniture is a page where extraction struggled, and being able to sort
    by this turns a vague worry into a number.
    """
    if not original:
        return 0.0
    removed = len(original) - len(cleaned)
    return max(0.0, min(1.0, removed / len(original)))
