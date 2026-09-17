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
import re
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


@pytest.fixture(scope="module")
def capture(client, populated):
    """A real hit and its replay info; skips only where no captures are mounted."""
    from causalia_extractor.identity import PAGES_ROOT
    if not PAGES_ROOT.is_dir():
        pytest.skip(f"no captures at {PAGES_ROOT} on this machine")
    hits = client.get("/search", params={"q": "kormány", "limit": 1}).json()["hits"]
    if not hits:
        pytest.skip("no hit to replay in this corpus")
    article_id = hits[0]["article"]["id"]
    r = client.get(f"/articles/{article_id}/replay")
    assert r.status_code == 200, r.text
    return article_id, r.json()


class TestArchiveReplay:
    """A hit leads back to the capture it was extracted from, replayable as captured."""

    def test_the_archive_endpoints_are_documented(self, client):
        paths = client.get("/openapi.json").json()["paths"]
        assert "/articles/{article_id}/replay" in paths
        assert "/articles/{article_id}/page.wacz" in paths

    def test_replay_info_is_in_the_form_the_player_needs(self, capture):
        article_id, info = capture
        assert re.fullmatch(r"\d{14}", info["ts"]), "replayweb.page fails silently on any other form"
        assert info["wacz_url"] == f"/articles/{article_id}/page.wacz"
        assert info["page_url"].startswith("http")
        assert info["article"]["id"] == article_id

    def test_the_capture_is_served_in_ranges(self, client, capture):
        _, info = capture
        r = client.get(info["wacz_url"], headers={"Range": "bytes=0-99"})
        assert r.status_code == 206
        assert r.headers["content-range"] == f"bytes 0-99/{info['wacz_bytes']}"
        assert r.content[:2] == b"PK", "a .wacz is a zip"

    def test_a_missing_article_has_no_capture(self, client):
        assert client.get("/articles/99999999/replay").status_code == 404
        assert client.get("/articles/99999999/page.wacz").status_code == 404


class TestResolve:
    """A link inside a replay opens our capture of its target - that article exactly, or nothing."""

    def test_resolve_is_documented(self, client):
        assert "/resolve" in client.get("/openapi.json").json()["paths"]

    def test_an_article_url_resolves_to_that_article(self, client, capture):
        article_id, info = capture
        r = client.get("/resolve", params={"url": info["page_url"]})
        assert r.status_code == 200, r.text
        assert r.json()["article"]["id"] == article_id

    def test_the_ways_a_link_can_differ_from_the_archived_url_still_resolve(self, client, capture):
        """What a link on the page may add or change without being another page."""
        from urllib.parse import urlsplit, urlunsplit
        article_id, info = capture
        p = urlsplit(info["page_url"])
        tracking = (p.query + "&" if p.query else "") + "utm_source=facebook&fbclid=x"
        for variant in (
            urlunsplit((p.scheme, p.netloc, p.path.rstrip("/") + "/", p.query, "comments")),
            urlunsplit((p.scheme, p.netloc, p.path, tracking, "")),
            urlunsplit(("http", p.netloc, p.path, p.query, "")),
            urlunsplit((p.scheme, "www." + p.netloc.removeprefix("www."), p.path, p.query, "")),
        ):
            r = client.get("/resolve", params={"url": variant})
            assert r.status_code == 200, variant
            assert r.json()["article"]["id"] == article_id, variant

    def test_another_page_on_the_same_site_does_not_resolve_to_it(self, client, capture):
        _, info = capture
        p = info["page_url"].rstrip("/")
        for other in (p + "-2", p.rsplit("/", 1)[0], p + "/comments"):
            assert client.get("/resolve", params={"url": other}).status_code == 404, other

    def test_a_page_we_do_not_hold_is_404(self, client):
        r = client.get("/resolve", params={"url": "https://example.invalid/2024/01/nothing-here"})
        assert r.status_code == 404

    def test_only_an_absolute_web_url_is_asked(self, client):
        for bad in ("javascript:alert(1)", "/belfold/2024/01/relative", "mailto:a@b.hu", "https://", ""):
            assert client.get("/resolve", params={"url": bad}).status_code in (400, 422), bad


class TestUI:
    def test_the_search_page_and_its_script_are_served(self, client):
        page = client.get("/")
        assert page.status_code == 200
        assert 'id="search-form"' in page.text
        assert client.get("/static/app.js").status_code == 200

    def test_the_page_and_its_scripts_are_always_revalidated(self, client):
        """Otherwise a browser keeps running the previous app.js after a deploy."""
        for path in ("/", "/static/app.js", "/static/app.css"):
            assert client.get(path).headers.get("cache-control") == "no-cache", path

    def test_the_scripts_the_replay_worker_injects_exist(self, client):
        """app.js names these only inside the worker URL; renaming one side would silently
        bring back the page-wiping anti-adblock redirect, or dead links, in every replay."""
        app_js = client.get("/static/app.js").text
        injected = re.search(r"injectScripts=([^&\"]+)", app_js)
        assert injected, "app.js no longer asks the replay worker to inject anything"
        scripts = injected.group(1).split(",")
        assert {"/static/replay-guard.js", "/static/replay-links.js"} <= set(scripts)
        for script in scripts:
            r = client.get(script)
            assert r.status_code == 200, script
            assert "javascript" in r.headers["content-type"], script

    def test_the_link_notice_the_app_fills_is_on_the_page(self, client):
        page = client.get("/").text
        for element in ('id="replay-notice"', 'id="notice-url"', 'id="notice-open"', 'id="notice-close"'):
            assert element in page, element


class TestDateFilter:
    """Articles by publication date: alone through /articles, with words through /search.

    Days are the database's (UTC), and published_at comes back in that zone, so its
    first ten characters are the day the filter compared.
    """

    def test_a_month_lists_only_that_month_newest_first(self, client, populated):
        r = client.get("/articles", params={"from": "2024-03-01", "to": "2024-03-31", "limit": 50})
        assert r.status_code == 200, r.text
        body = r.json()
        dates = [a["published_at"] for a in body["articles"]]
        assert dates, "no articles published in March 2024"
        assert all("2024-03-01" <= d[:10] <= "2024-03-31" for d in dates)
        assert dates == sorted(dates, reverse=True)
        assert body["total_articles"] >= len(dates)

    def test_one_day_is_that_whole_day_and_nothing_else(self, client, populated):
        body = client.get("/articles", params={"from": "2024-03-15", "to": "2024-03-15",
                                               "limit": 100}).json()
        assert body["articles"], "no articles published on 2024-03-15"
        assert {a["published_at"][:10] for a in body["articles"]} == {"2024-03-15"}

    def test_a_listing_needs_a_date(self, client):
        assert client.get("/articles").status_code == 400

    def test_from_after_to_is_refused(self, client):
        r = client.get("/articles", params={"from": "2024-04-01", "to": "2024-03-01"})
        assert r.status_code == 400

    def test_a_malformed_date_is_refused(self, client):
        assert client.get("/articles", params={"from": "2024-13-45"}).status_code == 422

    def test_words_and_dates_together(self, client, populated):
        wide = client.get("/search", params={"q": "Zrínyi", "limit": 20}).json()
        narrow = client.get("/search", params={"q": "Zrínyi", "from": "2023-01-01",
                                               "to": "2023-12-31", "limit": 20}).json()
        assert narrow["hits"], "no Zrínyi article published in 2023"
        assert narrow["total_articles"] <= wide["total_articles"]
        assert all(h["article"]["published_at"][:4] == "2023" for h in narrow["hits"])

    def test_the_page_has_the_date_fields(self, client):
        page = client.get("/").text
        assert 'id="from"' in page and 'id="to"' in page
