"""Regression tests for the search layer, against a real migrated database.

WHY THIS FILE EXISTS

Every other test in this suite is a pure-Python test of the extractor, and
``test_pipeline.py`` actively asserts the package never opens a socket. That is
correct and stays correct - but it left search with NO automated coverage at
all. Search lives in ``scripts/search.py`` and ``migrations/*.sql``, and its only
safety nets were a human remembering to run ``scripts/dev_validate.py`` or
``scripts/search_eval.py``, plus the ``DO $verify$`` blocks that fire once, when
a migration is applied, and never again.

The gap is not theoretical. Migration 009's ``corpus.phrase_match`` shipped a
defect - it bypassed 010's lemma guard and matched *orra* ("a nose") for the
needle *Orbán* - and nothing caught it until 014 went looking. 009 has no verify
block.

HOW IT STAYS HONEST ACROSS DIFFERENT CORPORA

The development database holds 36 articles and the evaluation database 1,008, so
pinning result COUNTS would make this file wrong on one of them the day it was
written. Two kinds of assertion are used instead:

  * **Corpus-independent** - ``corpus.search_vector('literal text')`` against
    ``corpus.search_query('query')``. The document is built inline, so the answer
    cannot depend on what happens to be ingested. Most of the file is this.
  * **Invariants** - relationships that must hold on ANY corpus (a phrase result
    is a subset of the bag-of-words result; every row a date filter returns is
    inside the range). These skip themselves when the corpus cannot exercise
    them, rather than failing on a small one.

Run it against a database migrated to at least 020::

    CX_TEST_DSN="host=127.0.0.1 port=55435 user=causalia password=eval \\
                 dbname=causalia_eval" .venv/bin/python -m pytest tests/test_search_db.py

With ``CX_TEST_DSN`` unset the whole module skips, so ``pytest`` on a fresh
checkout stays self-contained - the same contract ``test_integration.py`` uses
for its external archive directory.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

DSN = os.environ.get("CX_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="CX_TEST_DSN not set - no database to test search against")

psycopg2 = pytest.importorskip("psycopg2", reason="pip install -e '.[db]'")
import psycopg2.extras                                          # noqa: E402
import search as S                                              # noqa: E402

#: The migration that introduced the last behaviour this file pins.
REQUIRED_MIGRATION = "020"


@pytest.fixture(scope="module")
def conn():
    connection = S.connect(DSN)
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def cur(conn):
    with conn.cursor() as c:
        yield c


@pytest.fixture(scope="module")
def dcur(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as c:
        yield c


def matches_phrase(cur, document: str, needle: str) -> bool:
    """Does `document` contain `needle` as a phrase? Corpus-independent."""
    cur.execute("SELECT corpus.phrase_match(%s, %s)", (document, needle))
    return cur.fetchone()[0]


def matches(cur, document: str, query: str) -> bool:
    """Does `document` answer `query`? Built inline, so corpus-independent."""
    cur.execute("SELECT corpus.search_vector(%s) @@ corpus.search_query(%s)",
                (document, query))
    return cur.fetchone()[0]


def ids(dcur, query: str, **kw) -> set[int]:
    return {r["id"] for r in S.search_articles(dcur, query, limit=100000, **kw)}


def _rows(cur, sql, args=()):
    cur.execute(sql, args)
    return cur.fetchall()


@pytest.fixture(scope="module")
def probe(cur, dcur):
    """A query that actually matches a large slice of THIS corpus.

    Not a hardcoded word. The first draft of this file used ``"a"``, which is a
    Hungarian stopword: it matches nothing, so every filter invariant below
    passed vacuously against an empty set and one of them failed for a reason
    that had nothing to do with dates. A probe has to be shown to match before
    anything is concluded from filtering it - so this picks the corpus's own
    most frequent lexemes and returns the first that really retrieves.
    """
    cur.execute("""
        SELECT word FROM ts_stat('SELECT search_tsv FROM corpus.article')
        ORDER BY ndoc DESC LIMIT 40""")
    for (word,) in cur.fetchall():
        if len(S.search_articles(dcur, word, limit=2)) > 1:
            return word
    pytest.skip("no probe query retrieves anything from this corpus")


def test_the_probe_is_not_vacuous(probe, dcur):
    """Guards every invariant below: an empty set satisfies all of them."""
    assert len(ids(dcur, probe)) > 1, (
        f"probe {probe!r} retrieves too little to test filtering against")


# ---------------------------------------------------------------------

class TestHarness:
    def test_the_database_is_migrated_far_enough(self, cur):
        cur.execute("SELECT max(version) FROM corpus.schema_migrations")
        applied = cur.fetchone()[0]
        assert applied >= REQUIRED_MIGRATION, (
            f"database is at {applied}, these tests pin behaviour "
            f"introduced by {REQUIRED_MIGRATION}")


class TestFixedDefects:
    """One test per defect a migration was written to close.

    Each of these FAILED before its migration and must never fail again. They
    are the reason this file exists: a future change to the query pipeline that
    reopens one of them should turn this suite red, not wait for someone to
    re-run the yardstick by hand.
    """

    def test_010_an_accent_free_name_is_not_over_stemmed(self, cur):
        # 'Orban' had its -ban read as the inessive case and stemmed to 'or',
        # a lexeme matching 14% of the corpus.
        cur.execute("SELECT corpus.search_query('Orban')::text")
        assert "'or'" not in cur.fetchone()[0]

    def test_013_a_compound_is_not_satisfied_by_one_half(self, cur):
        assert matches(cur, "orosz-ukrán háború tört ki", "orosz-ukrán")
        assert not matches(cur, "az ukrán elnök beszélt", "orosz-ukrán")

    def test_015_a_closed_compound_answers_its_head_word(self, cur):
        assert matches(cur, "a koronavírusteszt eredménye", "koronavírus")

    def test_015_the_prefix_threshold_does_not_leak(self, cur):
        # The family that set the threshold at 7 rather than 6.
        assert not matches(cur, "a magyarázat egyszerű", "magyar")
        assert not matches(cur, "a kormány döntött", "kór")

    def test_017_typing_the_accent_narrows(self, cur):
        assert not matches(cur, "a kor jó volt", "kór")
        assert matches(cur, "a kór terjedt", "kór")

    def test_017_is_one_way_typing_without_it_does_not(self, cur):
        # Accent-insensitivity for the reader with no Hungarian keyboard. The
        # first draft of 020's verify block asserted the opposite and was wrong.
        assert matches(cur, "a kór terjedt", "kor")

    def test_018_an_accented_word_finds_its_own_inflections(self, cur):
        assert matches(cur, "a pártok megegyeztek", "párt")

    def test_019_an_accent_free_case_suffix_still_strips(self, cur):
        for document, query in [
            ("döntött a kormányról a testület", "kormanyrol"),
            ("kérdezte a kormánytól", "kormanytol"),
            ("tárgyalt a kormánynál", "kormanynal"),
            ("idézet a kormányból", "kormanybol"),
        ]:
            assert matches(cur, document, query), query

    def test_019_reaches_the_base_form_not_just_the_inflection(self, cur):
        assert matches(cur, "a kormány döntött", "kormanyrol")

    def test_019_leaves_a_term_that_already_stems_alone(self, cur):
        cur.execute("SELECT corpus.reaccented_lemmas('kormanynak')")
        assert cur.fetchone()[0] == []

    def test_020_an_accented_stopword_matches(self, cur):
        assert matches(cur, "Beszélt arról a kérdésről.", "arról")
        assert matches(cur, "Ezért döntött így a testület.", "ezért")

    def test_020_a_stopword_no_longer_annihilates_the_conjunction(self, cur):
        # 012 requires every term to appear somewhere, so a term matching
        # nothing made the whole query unsatisfiable and one function word took
        # the rest of the query down with it.
        assert matches(cur, "A kormány beszélt arról a kérdésről.",
                       "kormány arról")

    def test_020_did_not_collapse_the_accent_distinction(self, cur):
        cur.execute("SELECT corpus.accented_query('kór')::text")
        assert "'kor'" not in cur.fetchone()[0]


class TestConjunctionAndPhrase:
    def test_every_term_must_appear_somewhere(self, cur):
        assert matches(cur, "a kormány és a büdzsé", "kormány büdzsé")
        assert not matches(cur, "csak a kormány", "kormány büdzsé")

    def test_the_and_across_terms_survives_prefixing(self, cur):
        assert not matches(cur, "kormányzati energiapolitika",
                           "kormány büdzsé")

    def test_phrase_requires_adjacency(self, cur):
        cur.execute("SELECT corpus.phrase_match(%s, %s)",
                    ("Orbán Viktor beszélt", "Orbán Viktor"))
        assert cur.fetchone()[0]
        cur.execute("SELECT corpus.phrase_match(%s, %s)",
                    ("Viktor és Orbán", "Orbán Viktor"))
        assert not cur.fetchone()[0]

    def test_014_phrase_match_does_not_match_a_nose_for_orban(self, cur):
        # The defect 009 shipped and 014 closed. 009 has no verify block, so
        # this assertion is the only automated guard on it.
        cur.execute("SELECT corpus.phrase_match(%s, %s)",
                    ("beütötte az orra hegyét", "Orban"))
        assert not cur.fetchone()[0]

    def test_phrase_results_are_a_subset_of_bag_of_words(self, dcur):
        for query in ["Orbán Viktor", "Európai Unió", "Magyarország kormánya"]:
            loose, strict = ids(dcur, query), ids(dcur, query, phrase=True)
            assert strict <= loose, query


class TestFilters:
    """The filter paths dev_validate.py never exercises.

    Written as invariants over whatever the corpus contains, so they hold on the
    36-article development database and the 1,008-article evaluation one alike.
    """

    def _any_row(self, cur, sql):
        cur.execute(sql)
        return cur.fetchone()

    def test_author_filter_returns_only_that_author(self, cur, dcur):
        row = self._any_row(cur, """
            SELECT authors[1] FROM corpus.article
            WHERE cardinality(authors) > 0 LIMIT 1""")
        if not row:
            pytest.skip("no article in this corpus has an author")
        author = row[0]
        rows = S.filter_by_author(dcur, author, limit=100000)
        assert rows, f"exact filter found nothing for {author!r}"
        assert all(author in r["authors"] for r in rows)

    def test_tag_filter_returns_only_that_tag(self, cur, dcur):
        row = self._any_row(cur, """
            SELECT tags[1] FROM corpus.article
            WHERE cardinality(tags) > 0 LIMIT 1""")
        if not row:
            pytest.skip("no article in this corpus has a tag")
        tag = row[0]
        rows = S.filter_by_tag(dcur, tag, limit=100000)
        assert rows
        assert all(tag in r["tags"] for r in rows)

    def test_date_from_excludes_everything_earlier(self, cur, dcur, probe):
        row = self._any_row(cur, """
            SELECT date(min(published_at) + interval '1 day')
            FROM corpus.article WHERE published_at IS NOT NULL""")
        if not row or not row[0]:
            pytest.skip("no dated articles in this corpus")
        cutoff = row[0]
        hits = S.search_articles(dcur, probe, limit=100000,
                                 date_from=str(cutoff))
        for hit in hits:
            assert hit["published_at"] is not None
            assert hit["published_at"].date() >= cutoff

    def test_date_to_is_inclusive_of_the_day_given(self, cur, dcur, probe):
        row = self._any_row(cur, """
            SELECT date(max(published_at)) FROM corpus.article
            WHERE published_at IS NOT NULL""")
        if not row or not row[0]:
            pytest.skip("no dated articles in this corpus")
        last = row[0]
        hits = S.search_articles(dcur, probe, limit=100000, date_to=str(last))
        assert all(h["published_at"].date() <= last for h in hits)
        # An article published ON the boundary day must survive it.
        # Only meaningful if an article on the boundary day is reachable by
        # the probe at all - otherwise its absence says nothing about dates.
        on_last = {i for i in ids(dcur, probe)} & {
            r[0] for r in _rows(cur, """SELECT id FROM corpus.article
                                        WHERE date(published_at) = %s""", (last,))}
        if on_last:
            same_day = [h for h in hits if h["published_at"].date() == last]
            assert same_day, "date_to dropped the day it names"

    def test_a_filter_only_ever_narrows(self, cur, dcur, probe):
        row = self._any_row(cur, "SELECT outlet FROM corpus.article LIMIT 1")
        if not row:
            pytest.skip("empty corpus")
        unfiltered = ids(dcur, probe)
        assert unfiltered, "vacuous"
        assert ids(dcur, probe, outlet=row[0]) <= unfiltered

    def test_filters_compose(self, cur, dcur, probe):
        row = self._any_row(cur, """
            SELECT outlet, tags[1] FROM corpus.article
            WHERE cardinality(tags) > 0 LIMIT 1""")
        if not row:
            pytest.skip("no tagged article in this corpus")
        outlet, tag = row
        both = ids(dcur, probe, outlet=outlet, tag=tag)
        assert both <= ids(dcur, probe, outlet=outlet)
        assert both <= ids(dcur, probe, tag=tag)


class TestRankingAndCitation:
    """The scripts/search.py code path, which no verify block reaches.

    Every DO $verify$ block tests SQL functions on literal fixtures. Ranking and
    the block/XPath payload live in Python and had no automated coverage at all.
    """

    def test_score_matches_the_ordering_it_claims(self, dcur):
        """Rows come back in descending score order, to float4 precision.

        Not exact equality. ``ts_rank`` returns Postgres ``real`` - float4, about
        7 significant digits - and the ORDER BY sums those in SQL while
        ``hit["score"]`` re-sums the values after they have crossed into Python.
        The two therefore disagree in the last bits, and two articles tied to 7
        figures can appear to invert by ~5e-08. The SQL ordering is the
        authority; the reconstructed score is a display value. An inversion
        LARGER than float4 epsilon would be a real misordering, and that is what
        this pins.
        """
        hits = S.search_articles(dcur, "kormány", limit=25)
        if len(hits) < 2:
            pytest.skip("corpus too small to order")
        for i in range(len(hits) - 1):
            hi, lo = hits[i]["score"], hits[i + 1]["score"]
            assert hi >= lo - max(abs(hi), 1.0) * 1e-6, (
                f"row {i} scores {hi!r} but sorts above {lo!r}")

    def test_score_is_the_sum_of_its_parts(self, dcur):
        for hit in S.search_articles(dcur, "kormány", limit=10):
            expected = (hit["term_rank"] + hit["accent_rank"]
                        + hit["meta_rank"] + hit["body_rank"]
                        + hit["caption_rank"])
            assert hit["score"] == pytest.approx(expected)

    def test_a_body_hit_carries_a_citable_block(self, dcur):
        for hit in S.search_articles(dcur, "kormány", limit=25):
            if hit["match_reason"] in ("body", "both"):
                assert hit["blocks"], "a body hit produced no citable block"
                block = hit["blocks"][0]
                assert block["xpath"], "a citable block has no xpath"
                assert block["block_text"]
                return
        pytest.skip("no body hit in this corpus for the probe query")

    def test_block_search_returns_an_xpath_for_every_row(self, dcur):
        rows = S.search_article_content(dcur, "kormány", limit=25)
        if not rows:
            pytest.skip("no block hits in this corpus")
        assert all(r["xpath"] for r in rows)

    def test_matching_ids_is_unlimited(self, dcur, probe):
        # The bug this guards: comparing a LIMIT-truncated ranked list against
        # an unlimited yardstick reported 546 recall misses where there were 13.
        assert ids(dcur, probe) >= {
            h["id"] for h in S.search_articles(dcur, probe, limit=5)}


class TestUnsupportedSyntax:
    """Operator-shaped input must be refused, not silently searched for."""

    @pytest.mark.parametrize("query", [
        "kormány OR Ukrajna", "kormány NOT Ukrajna", "kormány AND Ukrajna",
        "kormány -Ukrajna", "korm*", "title:kormány", "kormány | Ukrajna",
    ])
    def test_operator_syntax_is_refused(self, dcur, query):
        with pytest.raises(S.UnsupportedQuerySyntax):
            S.search_articles(dcur, query, limit=1)

    @pytest.mark.parametrize("query", [
        "Európa-bajnokság",        # internal hyphen is a compound, not exclusion
        "Orbán Viktor: nem szabad",  # attribution colon is not field-scoping
        "COVID-19", "koronavírusteszt", "kormány Ukrajna", "Orbán Viktor",
    ])
    def test_legitimate_hungarian_is_not_refused(self, dcur, query):
        S.search_articles(dcur, query, limit=1)   # must not raise

    def test_every_real_title_is_still_searchable(self, cur):
        """The false-positive rate of the refusal, measured on real data."""
        cur.execute("SELECT title FROM corpus.article WHERE title IS NOT NULL")
        rejected = [t for (t,) in cur.fetchall() if S.unsupported_syntax(t)]
        assert not rejected, f"{len(rejected)} real titles would be refused"


class TestSchemaGuard:
    def test_a_database_behind_the_checkout_is_refused(self, conn, monkeypatch):
        monkeypatch.setattr(S, "MIGRATIONS_DIR", str(
            Path(__file__).resolve().parent.parent / "migrations"))
        expected = S.expected_migrations()
        applied = S.applied_migrations(conn)
        assert not expected - applied, (
            "the test database is behind this checkout; migrate it first")


class TestPhraseSemantics:
    """The contract migration 021 gave ``corpus.phrase_match``.

    These are corpus-independent: every document is built inline, so the answer
    cannot depend on what happens to be ingested.

    ``phrase_match`` is the EXACT layer behind the GIN candidate filter. Before
    021 it was not: a Hungarian function word at either end of a phrase was
    dropped by the lemma configuration's stopword list, which silently turned a
    two-word phrase into a one-word query, and an accented needle could reach
    accent-folded text through the surface branch, contradicting 017. The
    pipeline only looked right because the candidate filter's stricter accent
    policy overrode it upstream.
    """

    # -- adjacency, including across Hungarian function words --------------

    def test_a_trailing_stopword_still_requires_adjacency(self, cur):
        # Defect A. 'arról' is a stopword in the lemma configuration, so
        # phraseto_tsquery dropped it and the phrase became just 'kormány'.
        assert not matches_phrase(
            cur, "A kormány döntött. Erről semmit nem mondott arról a kérdésről.",
            "kormány arról")

    def test_a_leading_stopword_still_requires_adjacency(self, cur):
        assert not matches_phrase(
            cur, "arról beszélt, majd a kormány döntött", "arról kormány")

    def test_an_adjacent_stopword_phrase_still_matches(self, cur):
        assert matches_phrase(cur, "a kormány arról beszélt", "kormány arról")

    def test_the_accent_free_spelling_of_a_stopword_phrase_matches(self, cur):
        assert matches_phrase(cur, "a kormany arrol beszelt", "kormany arrol")

    def test_an_interior_stopword_is_not_collateral_damage(self, cur):
        # An interior stopword becomes a <N> gap, which is correct. Only edge
        # stopwords break the phrase, and the guard must tell them apart.
        assert matches_phrase(cur, "a kormány arról döntött",
                              "kormány arról döntött")

    # -- accent policy, which 017 makes one-way ----------------------------

    def test_an_accented_needle_does_not_reach_folded_text(self, cur):
        # Defect B. Found on real blocks: Ludovic Orban, the Romanian
        # politician, was answering a search for Orbán.
        assert not matches_phrase(cur, "Ludovic Orban Romaniaban", "Orbán")
        assert not matches_phrase(cur, "a kor jo volt", "kór")

    def test_an_accent_free_needle_still_reaches_accented_text(self, cur):
        # The other direction must NOT close: 017 is one-way, and this is what
        # a reader with no Hungarian keyboard depends on.
        assert matches_phrase(cur, "Orbán Viktor Brüsszelben tárgyalt", "Orban")
        assert matches_phrase(cur, "Orban Viktor Brusszelben targyalt", "Orban")

    def test_an_accented_needle_finds_its_own_spelling(self, cur):
        assert matches_phrase(cur, "a kór terjedt", "kór")

    # -- what 014 established and 021 must not have broken -----------------

    def test_014_a_nose_is_not_a_prime_minister(self, cur):
        assert not matches_phrase(cur, "az apja orra a tóban", "Orban")

    def test_inflection_is_still_tolerated(self, cur):
        assert matches_phrase(cur, "Orbánnak üzent a miniszter", "Orbán")

    def test_order_decides(self, cur):
        assert matches_phrase(cur, "Orbán Viktor Brüsszelben tárgyalt",
                              "Orbán Viktor")
        assert not matches_phrase(cur, "Orbán Viktor Brüsszelben tárgyalt",
                                  "Viktor Orbán")

    def test_a_hyphenated_compound_is_a_phrase_and_keeps_its_order(self, cur):
        assert matches_phrase(cur, "az orosz-ukrán háború", "orosz-ukrán háború")
        assert not matches_phrase(cur, "az orosz-ukrán háború",
                                  "háború orosz-ukrán")

    def test_null_handling_is_unchanged(self, cur):
        # phrase_match is deliberately not STRICT - it decides its own answer.
        cur.execute("SELECT corpus.phrase_match(NULL, 'kormány'),"
                    "       corpus.phrase_match('a kormány döntött', NULL)")
        assert cur.fetchone() == (False, False)

    # -- the edge guard itself ---------------------------------------------

    @pytest.mark.parametrize("needle,survives", [
        ("kormány arról",         False),   # trailing stopword
        ("arról kormány",         False),   # leading stopword
        ("arról",                 False),   # nothing but a stopword
        ("kormány arról döntött", True),    # interior - becomes a <N> gap
        ("kormány kormány",       True),    # a repeat is two tokens, not one
        ("orosz-ukrán háború",    True),    # compound expands, edges intact
        ("kormány Ukrajna",       True),
    ])
    def test_phrase_edges_survive(self, cur, needle, survives):
        cur.execute("SELECT corpus.phrase_edges_survive("
                    "'corpus.hungarian_lemma', %s)", (needle,))
        assert cur.fetchone()[0] is survives


class TestCandidateFilterInvariant:
    """The architectural invariant the whole pipeline rests on.

    The GIN candidate filter is allowed to over-produce. It must never
    under-produce: a block the exact layer accepts has to be inside the
    candidate set, or the pipeline silently loses it. Before 021 this failed on
    real corpus blocks - 2 for 'Orbán', 40 for 'kór', 321 for 'kormány arról' -
    though in every case it was phrase_match that was wrong, not the filter.
    """

    @pytest.mark.parametrize("query", [
        "Orbán", "Orban", "Orbánnak", "Magyarországról",
        "kormány arról", "orosz-ukrán háború", "kór", "Európai Unió",
    ])
    def test_no_exact_match_falls_outside_the_candidate_set(self, cur, query):
        cur.execute("""
            SELECT count(*) FROM corpus.content_block b
             WHERE corpus.phrase_match(b.block_text, %(q)s)
               AND NOT (b.text_tsv @@ corpus.search_query(%(q)s))
        """, {"q": query})
        missed = cur.fetchone()[0]
        assert missed == 0, (
            f"{missed} block(s) satisfy phrase_match but are not candidates - "
            f"the filter is under-producing for {query!r}")


class TestPartBRegressions:
    """The query set the phrase-search review named explicitly.

    Corpus-independent wherever possible: the document is built inline, so the
    assertion cannot drift with what happens to be ingested.
    """

    @pytest.mark.parametrize("stopword", [
        "arról", "között", "előtt", "saját", "ezért", "által",
    ])
    def test_a_stopword_phrase_requires_adjacency(self, cur, stopword):
        """The 021 defect class, across the function words that exhibit it."""
        adjacent = f"a kormány {stopword} beszélt"
        apart = f"A kormány döntött. Semmit nem mondott {stopword} a kérdésről."
        needle = f"kormány {stopword}"
        assert matches_phrase(cur, adjacent, needle), f"adjacent {needle!r}"
        assert not matches_phrase(cur, apart, needle), f"non-adjacent {needle!r}"

    def test_orban_accent_policy_is_one_way(self, cur):
        # Typing the accent narrows; typing without it does not.
        assert not matches_phrase(cur, "Ludovic Orban Romaniaban", "Orbán")
        assert matches_phrase(cur, "Orbán Viktor Brüsszelben tárgyalt", "Orban")

    def test_orbannak_reaches_the_base_form(self, cur):
        assert matches_phrase(cur, "Orbánnak üzent a miniszter", "Orbán")

    def test_magyarorszagrol_case_suffix(self, cur):
        assert matches(cur, "Magyarországról érkezett a hír", "Magyarországról")
        assert matches(cur, "Magyarországról érkezett a hír", "Magyarorszagrol")

    def test_europai_unio_is_a_phrase(self, cur):
        assert matches_phrase(cur, "az Európai Unió döntése", "Európai Unió")
        assert not matches_phrase(cur, "az Unió és az Európai Tanács",
                                  "Európai Unió")

    def test_kor_and_kór_stay_apart(self, cur):
        assert not matches_phrase(cur, "a kor jo volt", "kór")
        assert matches_phrase(cur, "a kór terjedt", "kór")
        # ...but the accent-free spelling still reaches both, by design.
        assert matches_phrase(cur, "a kór terjedt", "kor")


class TestTokenisationSemantics:
    """Hyphens and compounds - PINNED DELIBERATELY, not inherited.

    These assertions encode a product decision, so that Postgres's parser cannot
    change the product's meaning by accident. They record what the engine does
    TODAY. If the team decides a dash variant should be interchangeable, or that
    a phrase should be findable inside a compound, these are the tests to change
    first - and their failure is then the signal that the decision was acted on,
    not a regression.

    Measured on the evaluation corpus: treating every dash variant as
    interchangeable would recover 17 further true positives out of 878 across a
    120-phrase probe set (roughly 1.9%).
    """

    def test_a_hyphenated_compound_matches_its_own_spelling(self, cur):
        assert matches_phrase(cur, "az orosz-ukrán háború kitört",
                              "orosz-ukrán háború")

    def test_an_en_dash_is_currently_NOT_the_same_as_a_hyphen(self, cur):
        """CURRENT BEHAVIOUR, and a decision the team may reverse.

        'orosz–ukrán' (U+2013) and 'orosz-ukrán' (U+002D) tokenise differently:
        the hyphen produces a compound plus its parts, the en dash produces only
        the parts. A Hungarian newsroom uses both typographies for one word.
        """
        assert not matches_phrase(cur, "az orosz–ukrán háború kitört",
                                  "orosz-ukrán háború")

    def test_a_phrase_is_currently_NOT_found_inside_a_compound(self, cur):
        """CURRENT BEHAVIOUR, and a decision the team may reverse.

        'Orbán Viktor-fóbia' tokenises 'Viktor-fóbia' as a compound, so the
        phrase 'Orbán Viktor' does not occur inside it.
        """
        assert not matches_phrase(
            cur, "Már megint elhatalmasodott rajtad az Orbán Viktor-fóbia!!",
            "Orbán Viktor")


class TestKnownGapAccentFreeStopwordPhrase:
    """A REGRESSION 021 INTRODUCED, recorded so it cannot be forgotten.

    Shape: an accent-free needle, an edge stopword, and accented text.

        phrase_match('a felek között van a vita', 'kozott van')   should be True

    Every branch declines it, each for a locally correct reason:

      * lemma   - 'van' is a stopword, so the edge guard skips the branch
      * folded  - hungarian_surface ALSO stopwords 'van', so the edge guard
                  skips this one too
      * exact   - `simple` is accent-preserving, so 'kozott' does not reach
                  'között'

    Before 021 this returned True, but only through the degenerate query that
    021 exists to prevent - a false positive rather than a real match. So 021
    traded a false positive for a false negative in this one shape.

    Measured cost on the evaluation corpus: 18 of 878 reference true positives
    across a 120-phrase probe set. A validated fix exists - give the folded
    branch a stopword-free configuration by running `simple` over
    corpus.unaccent_immutable(...) instead of hungarian_surface - which takes
    phrase_match false negatives from 44 to 16, the remainder being the separate
    dash question in TestTokenisationSemantics.

    These are strict xfails: when the fix lands they XPASS, the suite goes red,
    and whoever landed it is forced to delete this class.
    """

    @pytest.mark.xfail(strict=True,
                       reason="021 regression: accent-free needle + edge "
                              "stopword cannot reach accented text")
    def test_accent_free_needle_reaches_accented_text_across_a_stopword(self, cur):
        assert matches_phrase(cur, "a felek között van a vita", "kozott van")

    @pytest.mark.xfail(strict=True,
                       reason="021 regression: same shape, different stopword")
    def test_the_same_shape_with_szamara(self, cur):
        assert matches_phrase(cur, "mindenki számára elérhető", "mindenki szamara")
