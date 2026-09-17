"""Causalia search and evidence API.

WHY THIS IS NOT IN causalia_extractor
-------------------------------------
The extractor package opens no socket and no database connection, and
tests/test_pipeline.py asserts it. This service does both, so it lives outside
the package for the same reason scripts/ does - it is a consumer of the corpus,
not part of the thing that builds it. It is installed through the optional
`api` extra so the extractor stays dependency-free.

WHAT THIS IS FOR
----------------
A search result that is only a link is a search engine. The point of Causalia is
that a result is a LOCATABLE PASSAGE: a paragraph, its position inside an
archived document, and a quote that can be re-verified later. Every response
here is shaped to preserve that chain:

    query -> article -> content block -> exact passage -> selector -> archive

So /search does not return article ids and relevance scores alone. It returns
the paragraphs that caused the match, each carrying the XPath that locates it in
the archived page.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
  * No writes. Every endpoint is a SELECT. Minting a citation is a read here -
    /blocks/{id}/passage COMPUTES a selector and hands it back; persisting one
    is a decision for whoever owns the citation lifecycle, not for a read API.
  * No query language of its own. It reuses scripts/search.py so there is
    exactly one implementation of what a query means. When that layer refuses
    operator syntax, this returns 400 with the same explanation rather than
    guessing.
  * No authentication. This is a read-only service over a private corpus on a
    private network. Putting a token in front of it is a deployment decision.

RUNNING IT
----------
    pip install -e '.[db,api]'
    CX_API_DSN="host=127.0.0.1 port=5432 user=causalia password=... dbname=causalia \\
                options='-c default_transaction_read_only=on'" \\
        uvicorn service.app:app --host 127.0.0.1 --port 8765

    http://127.0.0.1:8765/          search UI; a result replays its archived .wacz
    http://127.0.0.1:8765/docs      interactive OpenAPI

Replay runs in a service worker, which browsers allow only on a secure origin, so
open it as localhost - from another machine through `ssh -N -L 8765:127.0.0.1:8765`.
CAUSALIA_PAGES_ROOT locates the captures; CX_REPLAY_ASSETS holds replayweb.page's
ui.js and sw.js.
"""
from __future__ import annotations

import os
import re
import sys
from contextlib import asynccontextmanager, contextmanager
from datetime import date, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# scripts/ is not an installed package - it is the database-facing tooling, and
# the test suite reaches it the same way. One insert, in one place, so the query
# layer has exactly one implementation and this service cannot drift from it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import search as S                                              # noqa: E402

import psycopg2.extras                                          # noqa: E402
from psycopg2.pool import ThreadedConnectionPool                # noqa: E402

from causalia_extractor.identity import PAGES_ROOT, WACZ_NAME, archive_id_for  # noqa: E402

DSN_ENV = "CX_API_DSN"
_pool: ThreadedConnectionPool | None = None
STATIC_DIR = Path(__file__).resolve().parent / "static"
#: replayweb.page's ui.js and sw.js: the copy the archive's own viewers already use.
REPLAY_ASSETS = Path(os.environ.get("CX_REPLAY_ASSETS", "/mnt/hdd/c0cshf/causalia/pages/viewer"))
#: Both parts are joined into a filesystem path, so they must have exactly these shapes.
_URL_HASH = re.compile(r"[0-9a-f]{64}")
_OUTLET = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        raise RuntimeError(f"{DSN_ENV} is not set; the API has no corpus to serve")
    # One schema check at startup rather than per request. search.connect()
    # refuses a database behind this checkout, so a stale corpus fails loudly
    # here instead of silently answering every query the old way.
    S.connect(dsn).close()
    _pool = ThreadedConnectionPool(minconn=1, maxconn=8, dsn=dsn)
    try:
        yield
    finally:
        _pool.closeall()
        _pool = None


app = FastAPI(
    title="Causalia search and evidence API",
    version="0.1.0",
    summary="Retrieval over an archived news corpus, where every hit resolves "
            "to an exact passage in the archived page.",
    lifespan=lifespan,
)
#: The page and its scripts are revalidated on every load - a 304 when nothing changed.
#: Without a Cache-Control header a browser reuses its copy heuristically, so after a
#: deploy it served the new index.html with the previous app.js: the date fields showed
#: and did nothing.
NO_CACHE = {"Cache-Control": "no-cache"}


class _RevalidatedStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(NO_CACHE)
        return response


app.mount("/static", _RevalidatedStaticFiles(directory=STATIC_DIR), name="static")
# Same origin as the API and the captures, so the replay service worker can fetch both.
# html=True: the player's frame opens /replay/?source=... and needs index.html before the worker controls it.
app.mount("/replay", StaticFiles(directory=REPLAY_ASSETS, html=True, check_dir=False), name="replay")


@app.get("/", include_in_schema=False)
def ui():
    return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)


@contextmanager
def cursor(dict_rows: bool = True):
    if _pool is None:
        raise HTTPException(503, "the corpus connection pool is not initialised")
    conn = _pool.getconn()
    try:
        factory = psycopg2.extras.DictCursor if dict_rows else None
        with conn.cursor(cursor_factory=factory) as cur:
            # Measured 2026-09-14: JIT compile time is pure overhead on these
            # one-shot plans -- 10-75% faster across the full query mix, never
            # slower. See causalia-run/HANDOFF-SEARCH-OPTIMIZATION.md.
            cur.execute("SET LOCAL jit = off")
            yield cur
        conn.rollback()          # read-only: never leave a transaction open
    finally:
        _pool.putconn(conn)


def _cur():
    with cursor() as c:
        yield c


# ---------------------------------------------------------------------
# Response shapes. These are the contract, so they are explicit rather than
# dict-shaped: a client should be able to see from the schema alone that a hit
# carries a location, not just a score.
# ---------------------------------------------------------------------

class TextPosition(BaseModel):
    type: str = "TextPositionSelector"
    start: int
    end: int


class Selector(BaseModel):
    """W3C Web Annotation shape, matching corpus.passage_selector.

    The XPath locates the element; the character range locates the passage
    inside it; `exact` is the authority. XPath is positional and drifts if the
    document is re-extracted, so re-resolution checks the quote, not the path.
    """
    type: str = "XPathSelector"
    value: str = Field(description="XPath of the block within the archived page")
    refinedBy: TextPosition | None = None
    exact: str | None = Field(default=None, description="the quoted text itself")


class Passage(BaseModel):
    block_id: int
    block_index: int
    block_type: str | None
    text: str
    highlight: str | None = Field(
        default=None, description="the matching fragment, « » marking the terms")
    selector: Selector


class ArticleRef(BaseModel):
    id: int
    url_hash: str
    outlet: str
    title: str | None
    published_at: str | None
    section: str | None
    tags: list[str] = []
    authors: list[str] = []
    source_url: str | None
    canonical_url: str | None


class SearchHit(BaseModel):
    article: ArticleRef
    score: float
    match_reason: str = Field(
        description="metadata | body | caption | both | document - which vector "
                    "caused the hit")
    passages: list[Passage] = Field(
        default=[],
        description="the paragraphs that caused the match, each locatable in "
                    "the archived page")


class SearchResponse(BaseModel):
    query: str
    total_articles: int = Field(
        description="complete match count, not the page size - so a caller can "
                    "page without re-running the query to discover the end")
    limit: int
    offset: int
    phrase: bool
    hits: list[SearchHit]


class Health(BaseModel):
    ok: bool
    migration: str
    articles: int
    content_blocks: int


class ReplayRef(BaseModel):
    article: ArticleRef
    wacz_url: str = Field(description="the article's original capture; answers Range requests")
    page_url: str = Field(description="the URL to open inside the capture")
    ts: str | None = Field(description="capture time as YYYYMMDDhhmmss, the form replayweb.page needs")
    captured_at: str | None
    wacz_bytes: int


# ---------------------------------------------------------------------

def _article_ref(row: dict) -> ArticleRef:
    return ArticleRef(
        id=row["id"], url_hash=row["url_hash"], outlet=row["outlet"],
        title=row.get("title"),
        published_at=row["published_at"].isoformat() if row.get("published_at") else None,
        section=row.get("section"),
        tags=list(row.get("tags") or []), authors=list(row.get("authors") or []),
        source_url=row.get("source_url"), canonical_url=row.get("canonical_url"))


#: ts_headline marks matched terms with these, per search.HEADLINE_OPTS - but it
#: takes ONE text-search configuration and the vectors are built from a union of
#: several, so a term matched through the lemma side comes back UNMARKED.
#: search.py calls that "the right way to be wrong": under-highlighting is
#: cosmetic, highlighting the wrong word would be a fabricated citation. The
#: consequence here is that the highlight cannot be trusted to locate anything,
#: so the character range is derived from the query itself instead.
_MARK = re.compile(r"«(.+?)»", re.S)


def _locate(text: str, query: str) -> tuple[int, str] | None:
    """Where in `text` the query actually appears, or None.

    Tries the whole query first, which is the phrase and full-paragraph case,
    then the longest single term. Case-insensitive because a headline may
    capitalise what the query did not. Returns None rather than guessing: a
    selector that points at approximately the right words is worse than one that
    admits it does not know, and the caller can always ask
    /blocks/{id}/passage for an exact span.
    """
    if not text or not query:
        return None
    hay = text.casefold()
    for candidate in [query] + sorted(re.split(r"[\s-]+", query), key=len, reverse=True):
        c = candidate.strip().casefold()
        if len(c) < 3:
            continue
        at = hay.find(c)
        if at >= 0:
            return at, text[at:at + len(candidate.strip())]
    return None


def _passage(block: dict, query: str | None = None) -> Passage:
    """A block, plus a selector locating it - and the matched span when known."""
    text = block.get("block_text") or ""
    refined, exact = None, None
    found = _locate(text, query) if query else None
    if found:
        at, exact = found
        refined = TextPosition(start=at, end=at + len(exact))
    return Passage(
        block_id=block["block_id"], block_index=block["block_index"],
        block_type=block.get("block_type"), text=text,
        highlight=block.get("headline"),
        selector=Selector(value=block["xpath"], refinedBy=refined, exact=exact))


# ---------------------------------------------------------------------

@app.get("/health", response_model=Health, tags=["ops"])
def health(cur=Depends(_cur)):
    cur.execute("SELECT max(version) AS v FROM corpus.schema_migrations")
    migration = cur.fetchone()["v"]
    cur.execute("SELECT count(*) AS n FROM corpus.article")
    articles = cur.fetchone()["n"]
    cur.execute("SELECT count(*) AS n FROM corpus.content_block")
    blocks = cur.fetchone()["n"]
    return Health(ok=True, migration=migration, articles=articles,
                  content_blocks=blocks)


@app.get("/search", response_model=SearchResponse, tags=["search"])
def search(
    q: str = Query(description="words to find; all of them must appear"),
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    phrase: bool = Query(False, description="require the words adjacent, in order"),
    outlet: str | None = None,
    tag: str | None = Query(None, description="exact tag, not a text match"),
    author: str | None = Query(None, description="exact byline, not a text match"),
    section: str | None = None,
    published_from: str | None = Query(None, alias="from", description="YYYY-MM-DD"),
    published_to: str | None = Query(None, alias="to",
                                     description="YYYY-MM-DD, inclusive"),
    passages_per_article: int = Query(3, ge=0, le=20),
    cur=Depends(_cur),
):
    """Search the corpus. Every hit carries the paragraphs that caused it."""
    filters: dict[str, Any] = dict(
        outlet=outlet, tag=tag, author=author, section=section,
        date_from=published_from, date_to=published_to, phrase=phrase)
    try:
        rows = S.search_articles(cur, q, limit=limit, offset=offset,
                                 blocks_per_article=passages_per_article,
                                 **filters)
        # The complete count, so a client can page without guessing where the
        # end is. search_articles() already carries it on every row (a window
        # over the same candidates it scores and sorts, before LIMIT/OFFSET) --
        # cheaper than matching_ids() re-running the whole candidate search a
        # second time just to call len() on it. The one case that window can't
        # cover is offset landing past the last match, which returns no rows
        # and so no total; matching_ids stays deliberately unlimited for that.
        if rows:
            total = rows[0]["total_matches"]
        elif offset == 0:
            total = 0
        else:
            total = len(S.matching_ids(cur, q, **filters))
    except S.UnsupportedQuerySyntax as exc:
        # The refusal is the query layer's, not this service's. Passing the
        # explanation through unchanged keeps one source of truth for what a
        # query means.
        raise HTTPException(400, str(exc)) from None

    return SearchResponse(
        query=q, total_articles=total, limit=limit, offset=offset, phrase=phrase,
        hits=[SearchHit(article=_article_ref(r), score=float(r["score"]),
                        match_reason=r["match_reason"],
                        passages=[_passage(b, q) for b in r["blocks"]])
              for r in rows])


class ArticleList(BaseModel):
    total_articles: int = Field(
        description="every searchable article published in the range, not the page size")
    limit: int
    offset: int
    date_from: str | None
    date_to: str | None
    articles: list[ArticleRef]


#: The bounds scripts/search.py's MATCH_WHERE puts on a search, so a date range means
#: the same thing with words and without them: `to` covers that whole day, and days
#: are the database's (UTC). searchable_article is the same gate a search goes through.
_PUBLISHED_IN = """
      FROM corpus.article a
      JOIN corpus.searchable_article sa ON sa.id = a.id
     WHERE (%(date_from)s IS NULL OR a.published_at >= %(date_from)s::timestamptz)
       AND (%(date_to)s IS NULL OR a.published_at < (%(date_to)s::date + 1)::timestamptz)"""


@app.get("/articles", response_model=ArticleList, tags=["corpus"])
def articles_by_date(
    date_from: date | None = Query(None, alias="from", description="YYYY-MM-DD"),
    date_to: date | None = Query(None, alias="to", description="YYYY-MM-DD, inclusive"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    cur=Depends(_cur),
):
    """Articles published in a date range, newest first - the listing without words.

    With words, /search takes the same `from` / `to`.
    """
    if date_from is None and date_to is None:
        raise HTTPException(400, "give a from date, a to date, or both (YYYY-MM-DD)")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(400, f"the from date ({date_from}) is after the to date ({date_to})")
    params = {"date_from": date_from, "date_to": date_to, "limit": limit, "offset": offset}
    cur.execute("SELECT count(*) AS n" + _PUBLISHED_IN, params)
    total = cur.fetchone()["n"]
    cur.execute("""
        SELECT a.id, a.url_hash, a.outlet, a.title, a.published_at, a.section,
               a.tags, a.authors, a.source_url, a.canonical_url""" + _PUBLISHED_IN + """
         ORDER BY a.published_at DESC NULLS LAST, a.id
         LIMIT %(limit)s OFFSET %(offset)s""", params)
    return ArticleList(
        total_articles=total, limit=limit, offset=offset,
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None,
        articles=[_article_ref(r) for r in cur.fetchall()])


@app.get("/articles/{article_id}", response_model=ArticleRef, tags=["corpus"])
def article(article_id: int, cur=Depends(_cur)):
    cur.execute("""
        SELECT id, url_hash, outlet, title, published_at, section, tags,
               authors, source_url, canonical_url
          FROM corpus.article WHERE id = %s""", (article_id,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(404, f"no article {article_id}")
    return _article_ref(row)


def _capture(cur, article_id: int) -> tuple[Any, Path]:
    """The article row and its .wacz on this machine, or 404."""
    cur.execute("""
        SELECT id, url_hash, outlet, title, published_at, section, tags,
               authors, source_url, canonical_url, captured_at
          FROM corpus.article WHERE id = %s""", (article_id,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(404, f"no article {article_id}")
    if not (_URL_HASH.fullmatch(row["url_hash"] or "") and _OUTLET.fullmatch(row["outlet"] or "")):
        raise HTTPException(404, f"article {article_id} has no valid capture location")
    path = PAGES_ROOT / row["outlet"] / row["url_hash"][:2] / row["url_hash"] / WACZ_NAME
    if not path.is_file():
        raise HTTPException(404, f"the capture of article {article_id} is not on this machine")
    return row, path


@app.get("/articles/{article_id}/replay", response_model=ReplayRef, tags=["archive"])
def replay(article_id: int, cur=Depends(_cur)):
    """What a WACZ player needs to show the article exactly as it was captured."""
    row, path = _capture(cur, article_id)
    captured = row["captured_at"].astimezone(timezone.utc) if row["captured_at"] else None
    return ReplayRef(
        article=_article_ref(row), wacz_url=f"/articles/{article_id}/page.wacz",
        page_url=row["source_url"] or row["canonical_url"],
        ts=captured.strftime("%Y%m%d%H%M%S") if captured else None,
        captured_at=captured.isoformat() if captured else None,
        wacz_bytes=path.stat().st_size)


@app.api_route("/articles/{article_id}/page.wacz", methods=["GET", "HEAD"],
               response_class=FileResponse, tags=["archive"])
def wacz(article_id: int):
    """The article's original capture, byte for byte; answers Range requests."""
    with cursor() as cur:  # released before streaming: the player fetches many ranges at once
        _, path = _capture(cur, article_id)
    return FileResponse(path, media_type="application/wacz")


class Resolved(BaseModel):
    url: str = Field(description="the URL that was asked about")
    article: ArticleRef = Field(description="the archived article it points to; its capture is on this machine")


def _same_page_forms(url: str) -> list[str]:
    """The URL first, then the forms a site serves as the same page: https for http, and no www.

    archive_id_for() already ignores what else can differ in a link - tracking parameters,
    parameter order, the fragment, a trailing slash - so these are the only variants to try.
    """
    parts = urlsplit(url)
    host = parts.netloc.lower()
    forms = [url]
    for scheme in ("https", parts.scheme.lower()):
        for netloc in (host.removeprefix("www."), host):
            form = urlunsplit((scheme, netloc, parts.path, parts.query, ""))
            if form not in forms:
                forms.append(form)
    return forms


@app.get("/resolve", response_model=Resolved, tags=["archive"])
def resolve(
    url: str = Query(max_length=4096,
                     description="an absolute http(s) URL, e.g. a link inside an archived page"),
    cur=Depends(_cur),
):
    """Which archived article a URL points to, so a link inside a replay can open our copy.

    Matching is by the archive's own identity, the url_hash every capture is stored under -
    never by title or by canonical_url, because a near miss would open a different article.
    404 when we do not hold that page, or hold the row but not its capture.
    """
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise HTTPException(400, "give an absolute http(s) URL")
    hashes = [archive_id_for(form) for form in _same_page_forms(url.strip())]
    cur.execute("""
        SELECT id FROM corpus.article
         WHERE url_hash = ANY(%(hashes)s)
         ORDER BY array_position(%(hashes)s::text[], url_hash)
         LIMIT 1""", {"hashes": hashes})
    row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "that page is not in the archive")
    try:
        article_row, _ = _capture(cur, row["id"])
    except HTTPException:
        raise HTTPException(404, "that page is in the corpus, but its capture is not on this machine") from None
    return Resolved(url=url, article=_article_ref(article_row))


@app.get("/articles/{article_id}/blocks", response_model=list[Passage],
         tags=["corpus"])
def article_blocks(article_id: int, cur=Depends(_cur)):
    """Every citable block of an article, in document order.

    Restricted to the article's CURRENT extraction: superseded readings are not
    citable, because the passage they describe may no longer be there.
    """
    cur.execute("""
        SELECT b.id AS block_id, b.block_index, b.block_type, b.xpath,
               b.block_text, NULL::text AS headline
          FROM corpus.content_block b
          JOIN corpus.article a ON a.id = b.article_id
         WHERE b.article_id = %s
           AND b.extraction_id = a.current_extraction_id
         ORDER BY b.block_index""", (article_id,))
    rows = cur.fetchall()
    if not rows:
        raise HTTPException(404, f"no blocks for article {article_id}")
    return [_passage(dict(r)) for r in rows]


@app.get("/blocks/{block_id}", response_model=Passage, tags=["corpus"])
def block(block_id: int, cur=Depends(_cur)):
    cur.execute("""
        SELECT id AS block_id, block_index, block_type, xpath, block_text,
               NULL::text AS headline
          FROM corpus.content_block WHERE id = %s""", (block_id,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(404, f"no block {block_id}")
    return _passage(dict(row))


@app.get("/blocks/{block_id}/passage", response_model=Selector, tags=["evidence"])
def passage(
    block_id: int,
    quote: str = Query(description="exact text to locate inside the block"),
    cur=Depends(_cur),
):
    """Turn a quote into a citation: the selector that locates it in the archive.

    This is the end of the chain a search begins. The quote must appear in the
    block verbatim - if it does not, that is a 404 rather than a fuzzy match,
    because a citation that points at approximately the right words is worse
    than no citation.
    """
    cur.execute("SELECT xpath, block_text FROM corpus.content_block WHERE id = %s",
                (block_id,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(404, f"no block {block_id}")
    start = (row["block_text"] or "").find(quote)
    if start < 0:
        raise HTTPException(
            404, "that quote does not appear verbatim in this block; a citation "
                 "must point at text that is actually there")
    return Selector(value=row["xpath"],
                    refinedBy=TextPosition(start=start, end=start + len(quote)),
                    exact=quote)
