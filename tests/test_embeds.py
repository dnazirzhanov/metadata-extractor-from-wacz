"""Embed protection and adoption - readability.py.

Both `protect_embeds` and `adopt_raw_embeds` locate an embed's wrapper with
`embed_anchor` and replace it. The wrapper must belong to ONE embed, or the
first replacement carries the second embed out of the document and the second
replacement fails on a node that is no longer in a tree.

Found on a real capture: kisalfold.hu/00/0004ea40..., two infogram iframes
inside one <app-wysiwyg-box>. It crashed the extraction outright - the only
hard failure in 1,008 archives.
"""
from bs4 import BeautifulSoup

from causalia_extractor.readability import (
    EMBED_TOKEN, adopt_raw_embeds, embed_anchor, protect_embeds)

#: The shape that crashed: one wrapper, two players, no text between them.
TWO_IN_ONE_WRAPPER = """
<html><body><article>
  <p>Bevezető szöveg.</p>
  <app-wysiwyg-box>
    <iframe src="https://e.infogram.com/112a3429-10ca-4180-a160-4"></iframe>
    <iframe src="https://e.infogram.com/18eb57c2-3ce9-4cd7-8360-2"></iframe>
  </app-wysiwyg-box>
  <p>Záró szöveg.</p>
</article></body></html>
"""


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


class TestSharedWrapper:
    def test_two_embeds_in_one_wrapper_do_not_crash(self):
        embeds = protect_embeds(soup_of(TWO_IN_ONE_WRAPPER))
        assert len(embeds) == 2

    def test_both_embeds_keep_their_url(self):
        embeds = protect_embeds(soup_of(TWO_IN_ONE_WRAPPER))
        urls = {e.url for e in embeds.values()}
        assert urls == {"https://e.infogram.com/112a3429-10ca-4180-a160-4",
                        "https://e.infogram.com/18eb57c2-3ce9-4cd7-8360-2"}

    def test_every_recorded_embed_has_a_token_in_the_document(self):
        # An Embed in the map whose token is not in the HTML can never be
        # restored: restore_embeds walks tokens, not the map.
        soup = soup_of(TWO_IN_ONE_WRAPPER)
        embeds = protect_embeds(soup)
        html = str(soup)
        for index in embeds:
            assert EMBED_TOKEN.format(index=index) in html

    def test_the_anchor_is_not_shared_between_two_embeds(self):
        soup = soup_of(TWO_IN_ONE_WRAPPER)
        first, second = soup.find_all("iframe")
        assert embed_anchor(first) is not embed_anchor(second)

    def test_surrounding_prose_survives(self):
        soup = soup_of(TWO_IN_ONE_WRAPPER)
        protect_embeds(soup)
        text = soup.get_text(" ", strip=True)
        assert "Bevezető szöveg." in text
        assert "Záró szöveg." in text

    def test_adopt_raw_embeds_survives_the_same_shape(self):
        # The ng-state fallback path reaches this with the CMS's own markup.
        soup = soup_of(TWO_IN_ONE_WRAPPER)
        adopted = adopt_raw_embeds(soup, "https://kisalfold.hu/cikk")
        assert len(adopted) == 2
        assert len(soup.find_all("div", attrs={"data-embed-url": True})) == 2


class TestSoleEmbedStillGetsItsWrapper:
    def test_a_lone_embed_still_replaces_its_wrapper(self):
        # The original behaviour must not regress: one embed in one wrapper
        # still takes the wrapper with it, so no empty shell is left behind.
        soup = soup_of("""
            <html><body><article><p>Szöveg.</p>
              <div class="video-wrapper">
                <iframe src="https://www.youtube.com/embed/UwTYPHnSP8M"></iframe>
              </div>
            </article></body></html>""")
        embeds = protect_embeds(soup)
        assert len(embeds) == 1
        assert soup.find("div", class_="video-wrapper") is None

    def test_a_wrapper_holding_text_is_never_swallowed(self):
        soup = soup_of("""
            <html><body><article>
              <figure><iframe src="https://player.example/1"></iframe>
                      <figcaption>Aláírás</figcaption></figure>
            </article></body></html>""")
        protect_embeds(soup)
        assert "Aláírás" in soup.get_text(" ", strip=True)
