"""The read-only search and evidence API in service/.

Skips unless CX_TEST_DSN names a migrated database, exactly like
test_search_db.py, so pytest on a fresh checkout stays self-contained. The API
is exercised through Starlette's TestClient rather than a running server, so
these are fast and need no port.

What is asserted here is the CONTRACT, not the implementation: that a search hit
carries a locatable passage and not merely a score, that a citation is refused
rather than approximated when the quote is not really there, and that a query
the engine does not implement comes back as a 400 carrying the reason rather
than as a plausible wrong answer.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

DSN = os.environ.get("CX_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="CX_TEST_DSN not set - no corpus for the API to serve")

pytest.importorskip("fastapi", reason="pip install -e '.[api]'")
pytest.importorskip("psycopg2", reason="pip install -e '.[db]'")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="module")
def populated(client):
    """Skip when the schema is migrated but empty.

    CI builds the corpus schema on a fresh Postgres and ingests nothing, which
    is what makes the search job cheap. The endpoint contracts that need real
    articles skip themselves there rather than failing; the ones that do not -
    the OpenAPI schema, the 404s, the refusal of unsupported syntax - still run,
    and those are the ones that would catch a broken deployment.
    """
    n = client.get("/health").json()["articles"]
    if not n:
        pytest.skip("corpus is empty; endpoint contracts need articles")
    return n


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    os.environ["CX_API_DSN"] = DSN
    from service.app import app
    with TestClient(app) as c:
        yield c


class TestOps:
    def test_health_reports_the_corpus_it_is_serving(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        # The API refuses to start against a database behind the checkout, so a
        # served migration is always the current one.
        assert body["migration"] >= "023"
        # Not "> 0": health must answer on an empty corpus too, which is exactly
        # the state a deployment is in before its first ingest.
        assert body["articles"] >= 0
        assert body["content_blocks"] >= 0

    def test_the_openapi_schema_is_served(self, client):
        r = client.get("/openapi.json")
        assert r.status_code == 200
        paths = r.json()["paths"]
        for p in ("/search", "/articles/{article_id}", "/blocks/{block_id}",
                  "/blocks/{block_id}/passage"):
            assert p in paths


class TestSearchReturnsEvidence:
    """The point of the whole service: a hit is a location, not a link."""

    def test_a_hit_carries_the_paragraph_that_caused_it(self, client, populated):
        r = client.get("/search", params={"q": "kormány", "limit": 3})
        assert r.status_code == 200
        hits = r.json()["hits"]
        assert hits, "no hits for a word known to be in the corpus"
        with_passages = [h for h in hits if h["passages"]]
        assert with_passages, "every hit came back without a passage"
        p = with_passages[0]["passages"][0]
        assert p["selector"]["value"].startswith("/"), "no XPath on the passage"
        assert p["text"], "a passage with no text is not citable"

    def test_total_is_the_whole_match_not_the_page(self, client, populated):
        """A caller must be able to page without discovering the end by falling
        off it."""
        r = client.get("/search", params={"q": "kormány", "limit": 2}).json()
        assert r["total_articles"] > len(r["hits"])

    def test_paging_does_not_repeat_or_skip(self, client, populated):
        one = client.get("/search", params={"q": "kormány", "limit": 5,
                                            "offset": 0}).json()
        two = client.get("/search", params={"q": "kormány", "limit": 5,
                                            "offset": 5}).json()
        a = {h["article"]["id"] for h in one["hits"]}
        b = {h["article"]["id"] for h in two["hits"]}
        assert a and b and not (a & b)

    def test_phrase_narrows(self, client, populated):
        loose = client.get("/search", params={"q": "Viktor Orbán", "limit": 1}).json()
        strict = client.get("/search", params={"q": "Viktor Orbán", "limit": 1,
                                               "phrase": True}).json()
        assert loose["total_articles"] > 0
        assert strict["total_articles"] < loose["total_articles"], \
            "the reversed phrase should be far rarer than the bag of words"

    def test_filters_only_narrow(self, client, populated):
        wide = client.get("/search", params={"q": "kormány", "limit": 1}).json()
        narrow = client.get("/search", params={"q": "kormány", "limit": 1,
                                               "outlet": "feol.hu"}).json()
        assert narrow["total_articles"] <= wide["total_articles"]


class TestKnownGapPhraseAcrossFieldBoundary:
    """A defect this API's tests found, recorded rather than fixed.

    corpus.article's metadata recheck runs phrase_match over
    concat_ws(' ', title, subtitle, description). Those are three independent
    fields, so a phrase can match ACROSS the join - an adjacency the page never
    printed. Article 864 is a real example:

        title    '...tárgyal Orbán Viktor'
        subtitle 'Orbán Viktor miniszterelnök...'
        joined   '...tárgyal Orbán Viktor Orbán Viktor miniszterelnök...'
                                ^^^^^^^^^^^^^ 'Viktor Orbán' appears here only

    So a phrase search for 'Viktor Orbán' returns it, though neither field
    contains that order. META_HIT's own comment says joining is exactly the
    hazard it avoids for TAGS - "a phrase straddling two independent tags" - and
    the same reasoning applies to the prose fields, which are joined anyway.

    Impact is small: 2 of 88 for this query, and only at the metadata level;
    block-level phrase matching is unaffected because a block is one field. It
    is left for a migration of its own rather than smuggled into the service.
    """

    def test_a_phrase_can_straddle_title_and_subtitle(self, client, populated):
        r = client.get("/search", params={"q": "Viktor Orbán", "phrase": True,
                                          "limit": 5}).json()
        assert r["total_articles"] > 0, (
            "if this is now 0 the boundary defect has been fixed - delete this "
            "class and tighten test_phrase_narrows")


class TestTheEvidenceChain:
    """query -> article -> block -> exact passage -> selector."""

    def test_a_fragment_resolves_to_one_paragraph_and_its_selector(self, client, populated):
        frag = "tűzszünetet követelő tüntetők"
        hits = client.get("/search", params={"q": frag}).json()["hits"]
        assert len(hits) == 1
        passage = hits[0]["passages"][0]
        sel = passage["selector"]
        assert sel["refinedBy"] is not None, "no character range on the hit"
        assert sel["exact"]
        # the range must actually point at the quote inside the block text
        start, end = sel["refinedBy"]["start"], sel["refinedBy"]["end"]
        assert passage["text"][start:end].casefold() == sel["exact"].casefold()

    def test_a_quote_becomes_a_citation(self, client, populated):
        frag = "tűzszünetet követelő tüntetők"
        block_id = client.get("/search", params={"q": frag}).json()[
            "hits"][0]["passages"][0]["block_id"]
        r = client.get(f"/blocks/{block_id}/passage", params={"quote": frag})
        assert r.status_code == 200
        sel = r.json()
        assert sel["type"] == "XPathSelector"
        assert sel["refinedBy"]["end"] - sel["refinedBy"]["start"] == len(frag)
        assert sel["exact"] == frag

    def test_a_quote_that_is_not_there_is_refused_not_approximated(self, client, populated):
        """A citation pointing at approximately the right words is worse than
        no citation."""
        r = client.get("/blocks/35/passage",
                       params={"quote": "these words are not in that paragraph"})
        assert r.status_code == 404

    def test_article_blocks_are_in_document_order(self, client, populated):
        blocks = client.get("/articles/5/blocks").json()
        assert len(blocks) > 1
        idx = [b["block_index"] for b in blocks]
        assert idx == sorted(idx)
        assert all(b["selector"]["value"] for b in blocks)


class TestRefusalIsPassedThrough:
    @pytest.mark.parametrize("q", ["kormány OR Ukrajna", "kormány -Ukrajna",
                                   "title:kormány", "korm*"])
    def test_unsupported_syntax_is_a_400_with_the_reason(self, client, q):
        r = client.get("/search", params={"q": q})
        assert r.status_code == 400
        # the explanation comes from the query layer, not from this service
        assert "does not implement" in r.json()["detail"]

    def test_a_missing_article_is_404(self, client):
        assert client.get("/articles/99999999").status_code == 404

    def test_a_missing_block_is_404(self, client):
        assert client.get("/blocks/99999999").status_code == 404
