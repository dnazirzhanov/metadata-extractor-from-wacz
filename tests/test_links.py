"""links.json: every link addressed by a verified selector."""

from causalia_extractor import selectors as sel
from conftest import ARTICLE_URL, html_document, load, make_wacz, reader_tree

BODY = """
<div class="block-content">
<p>Az elso bekezdes eleg hosszu ahhoz, hogy a readability ezt a blokkot
   valassza ki a cikk torzsekent, es tartalmaz
   <a href="https://example.com/hir">egy kulso hivatkozast</a> is.</p>
<p>A masodik bekezdes egy <a href="https://ripost.hu/belfold/2026/01/masik">belso
   hivatkozast</a> tartalmaz, valamint <a href="mailto:x@y.hu">egy emailt</a>.</p>
</div>
"""


def run(tmp_path, body=BODY):
    from causalia_extractor.pipeline import extract
    wacz = make_wacz(tmp_path / "p" / "page.wacz", records=[
        {"uri": ARTICLE_URL, "content_type": "text/html",
         "body": html_document(body)}])
    result = extract(wacz, tmp_path / "out")
    assert result.output_dir is not None, result.error
    return result.output_dir


class TestFields:
    def test_links_are_described_with_their_context(self, tmp_path):
        directory = run(tmp_path)
        first = load(directory, "links.json")[0]
        assert first["url"] == "https://example.com/hir"
        assert first["text"] == "egy kulso hivatkozast"
        assert "elso bekezdes" in first["context"]

    def test_internal_and_external_links_are_distinguished(self, tmp_path):
        by_url = {r["url"]: r for r in load(run(tmp_path), "links.json")}
        assert by_url["https://example.com/hir"]["internal"] is False
        assert by_url["https://ripost.hu/belfold/2026/01/masik"]["internal"] is True

    def test_non_http_schemes_are_not_recorded(self, tmp_path):
        urls = {r["url"] for r in load(run(tmp_path), "links.json")}
        assert not any(u.startswith("mailto:") for u in urls)

    def test_position_and_block_id_are_gone(self, tmp_path):
        for record in load(run(tmp_path), "links.json"):
            assert "position" not in record
            assert "block_id" not in record


class TestSelectors:
    def test_every_selector_resolves_and_its_quote_matches(self, tmp_path):
        directory = run(tmp_path)
        tree = reader_tree(directory)
        records = load(directory, "links.json")
        assert records
        for record in records:
            assert sel.verify_payload(tree, record["selector"]) == sel.OK

    def test_the_quote_is_the_anchor_text(self, tmp_path):
        directory = run(tmp_path)
        for record in load(directory, "links.json"):
            assert record["selector"]["quote"]["exact"] == record["text"]

    def test_the_selector_points_at_the_owning_block_not_the_anchor(self, tmp_path):
        # A citation needs to say where in the prose the link sits; an offset
        # into the paragraph is what a highlighter can act on.
        directory = run(tmp_path)
        blocks = {b["xpath"] for b in load(directory, "content.json")["blocks"]}
        for record in load(directory, "links.json"):
            assert record["selector"]["value"] in blocks

    def test_the_offsets_land_on_the_anchor_text(self, tmp_path):
        from causalia_extractor.normalize import normalize_text
        from causalia_extractor.xpath import resolve
        directory = run(tmp_path)
        tree = reader_tree(directory)
        for record in load(directory, "links.json"):
            selector = record["selector"]
            text = normalize_text(resolve(tree, selector["value"]))
            start = selector["refinedBy"]["start"]
            end = selector["refinedBy"]["end"]
            assert text[start:end] == record["text"]

    def test_the_footer_link_is_not_an_article_link(self, tmp_path):
        # readability.html's footer carries the original URL. It is not prose.
        urls = {r["url"] for r in load(run(tmp_path), "links.json")}
        assert ARTICLE_URL not in urls

    def test_a_repeated_anchor_text_selects_distinct_ranges(self, tmp_path):
        body = """
<div class="block-content">
<p>Egy elegge hosszu bekezdes a readability kedveert, benne
   <a href="https://a.example/1">itt</a> es megint
   <a href="https://a.example/2">itt</a> is hivatkozas talalhato,
   plusz meg egy kis szoveg a vegen.</p>
</div>"""
        directory = run(tmp_path, body)
        records = load(directory, "links.json")
        starts = [r["selector"]["refinedBy"]["start"] for r in records]
        assert len(records) == 2
        assert starts[0] != starts[1]
        tree = reader_tree(directory)
        for record in records:
            assert sel.verify_payload(tree, record["selector"]) == sel.OK


class TestMediaBlocks:
    """The reader DOM writes its own anchor into every third-party player.

    ``dom._append_video`` renders an embed as ``<div class="embed"><a
    href=URL>platform: URL</a></div>``, because an offline page must not
    re-embed a player. Reading that anchor back as an article link is the
    extractor citing itself: on the corpus sample it was 35% of links.json.
    """

    IFRAME = ("<p>Egy elegge hosszu bekezdes, hogy a readability ezt a blokkot "
              "valassza ki a cikk torzsekent, es ne valami mast a lap szelerol.</p>"
              "<p>A masodik bekezdes szinten eleg hosszu ahhoz, hogy szamitson, "
              "es tartalmaz <a href='https://example.com/hir'>egy hivatkozast</a>.</p>"
              '<iframe src="https://www.youtube.com/embed/UwTYPHnSP8M"></iframe>')

    def test_the_readers_own_embed_anchor_is_not_an_article_link(self, tmp_path):
        body = '<div class="block-content">%s</div>' % self.IFRAME
        records = load(run(tmp_path, body), "links.json")
        assert [r["url"] for r in records] == ["https://example.com/hir"]

    def test_a_prose_link_beside_an_embed_is_still_recorded(self, tmp_path):
        body = '<div class="block-content">%s</div>' % self.IFRAME
        records = load(run(tmp_path, body), "links.json")
        assert records[0]["text"] == "egy hivatkozast"
        assert records[0]["context"] is not None
