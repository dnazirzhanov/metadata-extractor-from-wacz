"""original.html: the captured markup, with every network reference dead."""

import re

from causalia_extractor.original import build_original_html

PAGE = """<!DOCTYPE html>
<html><head>
<title>Cikk</title>
<link rel="stylesheet" href="https://cdn.example.com/site.css">
<script src="https://cdn.example.com/app.js"></script>
<script>window.dataLayer = [];</script>
<style>@import url("https://fonts.example.com/f.css"); body{color:red}</style>
<style>.local{color:blue}</style>
<base href="https://example.com/">
</head><body onload="boom()">
<p>Szoveg</p>
<img src="https://cdn.example.com/kep.jpg" data-src="https://cdn.example.com/nagy.jpg"
     alt="kep" onclick="track()">
<iframe src="https://www.youtube.com/embed/abc"></iframe>
<video poster="https://cdn.example.com/poster.jpg"><source src="https://cdn.example.com/v.mp4"></video>
</body></html>"""


class TestNothingFetches:
    def test_no_loadable_external_url_survives(self):
        result = build_original_html(PAGE)
        # Any http(s) URL left must not be in an attribute that causes a fetch.
        for match in re.finditer(r'(\w[\w-]*)\s*=\s*"(https?://[^"]*)"', result):
            assert match.group(1) not in (
                "src", "srcset", "poster", "data", "href"), match.group(0)

    def test_scripts_are_removed(self):
        assert "<script" not in build_original_html(PAGE)

    def test_external_stylesheets_are_removed(self):
        assert "site.css" not in build_original_html(PAGE)

    def test_a_stylesheet_that_imports_remotely_is_removed(self):
        assert "fonts.example.com" not in build_original_html(PAGE)

    def test_a_purely_local_style_block_is_kept(self):
        assert ".local" in build_original_html(PAGE)

    def test_base_is_removed_so_nothing_resolves_against_the_live_origin(self):
        assert "<base" not in build_original_html(PAGE)

    def test_inline_event_handlers_are_removed(self):
        result = build_original_html(PAGE)
        assert "onload" not in result and "onclick" not in result

    def test_a_no_referrer_policy_is_declared(self):
        assert 'content="no-referrer"' in build_original_html(PAGE)


class TestWhatIsPreserved:
    def test_the_article_text_survives(self):
        assert "Szoveg" in build_original_html(PAGE)

    def test_the_original_media_url_is_still_visible(self):
        # The file is an archival record: what the page meant to load is part
        # of what was captured.
        assert "kep.jpg" in build_original_html(PAGE)

    def test_alt_text_and_structure_survive(self):
        result = build_original_html(PAGE)
        assert 'alt="kep"' in result
        assert "<title>Cikk</title>" in result

    def test_it_is_deterministic(self):
        assert build_original_html(PAGE) == build_original_html(PAGE)
