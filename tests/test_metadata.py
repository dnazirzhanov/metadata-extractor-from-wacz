"""article.json: the cleaned metadata."""

from bs4 import BeautifulSoup

from causalia_extractor.metadata import _is_article_scoped_tag_link
from causalia_extractor.sites import rules_for

from conftest import ARTICLE_BODY, ARTICLE_URL, html_document, load, make_wacz


def extract_article(tmp_path, html, page_url=ARTICLE_URL):
    from causalia_extractor.pipeline import extract
    wacz = make_wacz(tmp_path / "p" / "page.wacz", page_url=page_url,
                     records=[{"uri": page_url, "content_type": "text/html",
                               "body": html}])
    result = extract(wacz, tmp_path / "out")
    assert result.output_dir is not None, result.error
    return load(result.output_dir, "article.json")


class TestFields:
    def test_the_declared_metadata_is_extracted(self, tmp_path):
        article = extract_article(tmp_path, html_document(ARTICLE_BODY))
        assert article["title"] == "Teszt cikk"
        assert article["description"] == "A teszt cikk leadje."
        assert article["author"] == ["Kovacs Anna"]
        assert article["publisher"] == "Ripost"
        assert article["published_at"] == "2026-06-01T09:00:00+02:00"
        assert article["updated_at"] == "2026-06-01T10:30:00+02:00"
        assert article["language"] == "hu"
        assert article["section"] == "sztardzsusz"
        assert article["canonical_url"] == ARTICLE_URL

    def test_identity_is_the_hash_the_rest_of_the_system_already_uses(self, tmp_path):
        article = extract_article(tmp_path, html_document(ARTICLE_BODY))
        assert article["outlet"] == "ripost.hu"
        assert len(article["archive_id"]) == 64

    def test_captured_at_comes_from_the_capture_not_the_page(self, tmp_path):
        article = extract_article(tmp_path, html_document(ARTICLE_BODY))
        assert article["captured_at"] == "2026-08-03T08:50:32.956Z"

    def test_source_and_canonical_url_are_both_kept(self, tmp_path):
        # They are not assumed equal: the crawled URL can redirect during
        # capture, and which one a citation refers to matters.
        html = html_document(ARTICLE_BODY).replace(
            f'<link rel="canonical" href="{ARTICLE_URL}">',
            '<link rel="canonical" href="https://ripost.hu/masik/2026/06/cikk">')
        article = extract_article(tmp_path, html)
        assert article["source_url"] == ARTICLE_URL
        assert article["canonical_url"] == "https://ripost.hu/masik/2026/06/cikk"

    def test_dates_are_kept_verbatim(self, tmp_path):
        # An ISO-8601 conversion that silently mangles a timezone is worse than
        # the original string.
        article = extract_article(tmp_path, html_document(ARTICLE_BODY))
        assert article["published_at"].endswith("+02:00")


class TestRemovedFields:
    def test_the_useless_fields_are_gone(self, tmp_path):
        article = extract_article(tmp_path, html_document(ARTICLE_BODY))
        for absent in ("site_name", "capture_title", "field_sources", "url_hash"):
            assert absent not in article

    def test_the_key_set_is_exactly_the_contract(self, tmp_path):
        article = extract_article(tmp_path, html_document(ARTICLE_BODY))
        assert set(article) == {
            "archive_id", "outlet", "title", "subtitle", "description",
            "author", "publisher", "source_url", "canonical_url",
            "published_at", "updated_at", "captured_at", "language",
            "section", "tags"}


class TestSubtitle:
    def test_an_alternative_headline_equal_to_the_title_is_rejected(self, tmp_path):
        # ripost.hu sets the two equal on essentially every article. A candidate
        # that resolves short-circuits the list, so accepting it hides the real
        # standfirst sitting in the DOM.
        html = html_document(
            '<div class="left-column"><p class="lead">A valodi felcim.</p>'
            '<div class="block-content"><p>Elso bekezdes hosszabb szoveggel, '
            'hogy a readability ezt valassza.</p><p>Masodik bekezdes.</p>'
            '</div></div>',
            extra_head='<meta name="x" content="y">')
        html = html.replace('"description":', '"alternativeHeadline": "Teszt cikk", "description":')
        article = extract_article(tmp_path, html)
        assert article["subtitle"] != article["title"]


class TestAuthors:
    def test_the_outlet_name_is_not_recorded_as_an_author(self, tmp_path):
        # A missing byline is a recorded absence; a wrong byline is a fabricated
        # claim about authorship, and this corpus is evidence.
        html = html_document(ARTICLE_BODY).replace(
            '"name": "Kovacs Anna"', '"name": "Ripost"')
        assert extract_article(tmp_path, html)["author"] == []

    def test_an_empty_name_is_not_an_author(self, tmp_path):
        html = html_document(ARTICLE_BODY).replace('"name": "Kovacs Anna"', '"name": ""')
        assert extract_article(tmp_path, html)["author"] == []


class TestTagScoping:
    """Which /cimke/ links are THIS article's tags.

    The same href pattern appears in three places that are not this article's
    tags: the site header or trending strip, the hamburger menu, and the tag
    chips printed on other articles' recommendation cards. Measured on this
    corpus: 82-222 such links per page against 0-18 real ones. So an anchor is
    admitted only if it reaches an article-scoped ancestor, and rejected outright
    inside a container that is about OTHER content.
    """

    RULES = rules_for("mandiner.hu")

    def scoped(self, html: str) -> bool:
        soup = BeautifulSoup(html, "lxml")
        return _is_article_scoped_tag_link(
            soup.find("a", href=True), self.RULES)

    def test_the_articles_own_tag_row_is_admitted_even_when_called_trending(self):
        # mandiner.hu puts this article's tags in div.trending-topics inside
        # section.article-page. Rejecting on the word cost it every tag on every
        # article; the tags are topical and differ per article, so it is not a
        # site strip. Measured 5 of 5 sampled articles.
        assert self.scoped(
            '<section class="article-page"><div class="wrapper with-aside">'
            '<man-trending-topics class="w-100"><div class="trending-topics">'
            '<a href="/cimke/zrinyi_miklos">Zrinyi Miklos</a>'
            '</div></man-trending-topics></div></section>')

    def test_the_site_wide_strip_in_the_hamburger_menu_is_rejected(self):
        # The same outlet's site strip - Orban Viktor, migracio, energiavalsag -
        # sits in header-hamburger-menu-left and reaches no article ancestor.
        assert not self.scoped(
            '<div class="header-hamburger-menu-container">'
            '<div class="header-hamburger-menu-left"><div class="header-trending-tag-box">'
            '<man-trending-tags><div class="trending-tags">'
            '<a href="/cimke/orban_viktor">Orban Viktor</a>'
            '</div></man-trending-tags></div></div></div>')

    def test_a_strip_that_reaches_no_article_ancestor_is_rejected(self):
        assert not self.scoped(
            '<div class="footer"><div class="trending-topics">'
            '<a href="/cimke/valami">valami</a></div></div>')

    def test_a_recommendation_cards_chips_are_rejected_inside_the_article_page(self):
        # This is what the reject list is FOR: a card is about another article
        # whatever it sits inside, so scope must not rescue it.
        assert not self.scoped(
            '<section class="article-page"><div class="recommend-box">'
            '<div class="article-card"><a href="/cimke/mas">mas cikk cimkeje</a>'
            '</div></div></section>')

    def test_a_related_box_is_rejected_inside_the_article_page(self):
        assert not self.scoped(
            '<section class="article-page"><div class="related-articles">'
            '<a href="/cimke/mas">mas</a></div></section>')

    def test_a_nav_element_is_rejected_on_the_element_name(self):
        # Rejecting on the token "header" would break metropol and magyarnemzet,
        # which keep real tags under article-header. The <nav> ELEMENT is the
        # safe structural signal.
        assert not self.scoped(
            '<article><nav class="article-nav">'
            '<a href="/cimke/valami">valami</a></nav></article>')

    def test_article_header_is_not_rejected(self):
        assert self.scoped(
            '<div class="article-header"><a href="/cimke/valami">valami</a></div>')

    def test_a_container_with_aside_in_its_class_is_not_rejected(self):
        # origo's real tag block sits under div.wrapper.narrow-wrapper.with-aside.
        assert self.scoped(
            '<div class="article-page"><div class="wrapper narrow-wrapper with-aside">'
            '<div class="article-tags"><a href="/cimke/valami">valami</a>'
            '</div></div></div>')

    def test_tags_reach_article_json_end_to_end(self, tmp_path):
        body = ('<div class="left-column"><div class="block-content">'
                '<p>Egy elegge hosszu bekezdes, hogy a readability ezt a blokkot '
                'valassza ki a cikk torzsekent, es ne valami mast a lap szelerol.</p>'
                '<p>Egy masodik bekezdes, szinten eleg hosszu ahhoz, hogy szamitson.</p>'
                '</div></div>'
                '<section class="article-page"><div class="trending-topics">'
                '<a href="https://ripost.hu/cimke/zrinyi">Zrinyi Miklos</a>'
                '<a href="https://ripost.hu/cimke/szigetvar">Szigetvar</a>'
                '</div></section>')
        article = extract_article(tmp_path, html_document(body))
        assert "Zrinyi Miklos" in article["tags"]
        assert "Szigetvar" in article["tags"]
