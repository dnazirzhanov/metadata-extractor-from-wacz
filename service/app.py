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
    CX_API_DSN="host=127.0.0.1 port=55435 user=causalia password=eval dbname=causalia_eval" \\
        uvicorn service.app:app --port 8000

    http://127.0.0.1:8000/docs      interactive OpenAPI
"""
from __future__ import annotations

import os
import re
import sys
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

# scripts/ is not an installed package - it is the database-facing tooling, and
# the test suite reaches it the same way. One insert, in one place, so the query
# layer has exactly one implementation and this service cannot drift from it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import search as S                                              # noqa: E402

import psycopg2.extras                                          # noqa: E402
from psycopg2.pool import ThreadedConnectionPool                # noqa: E402

DSN_ENV = "CX_API_DSN"
_pool: ThreadedConnectionPool | None = None


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


@contextmanager
def cursor(dict_rows: bool = True):
    if _pool is None:
        raise HTTPException(503, "the corpus connection pool is not initialised")
    conn = _pool.getconn()
    try:
        factory = psycopg2.extras.DictCursor if dict_rows else None
        with conn.cursor(cursor_factory=factory) as cur:
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
        # end is. matching_ids is deliberately unlimited for exactly this.
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
