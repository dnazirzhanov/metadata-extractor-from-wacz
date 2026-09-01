"""videos.json, the local videos/ directory, and their link to content blocks."""

from conftest import ARTICLE_URL, MP4_BODY, html_document, load, make_wacz

PROSE = ("<p>Egy elegge hosszu bekezdes, hogy a readability ezt a blokkot "
         "valassza ki a cikk torzsekent, es ne valami mast a lap szelerol.</p>"
         "<p>Egy masodik bekezdes, szinten eleg hosszu ahhoz, hogy szamitson "
         "a jelolt kivalasztasakor.</p>")


def run(tmp_path, body, extra_records=()):
    from causalia_extractor.pipeline import extract
    wacz = make_wacz(tmp_path / "p" / "page.wacz", records=[
        {"uri": ARTICLE_URL, "content_type": "text/html",
         "body": html_document(f'<div class="block-content">{body}</div>')},
        *extra_records])
    result = extract(wacz, tmp_path / "out")
    assert result.output_dir is not None, result.error
    return result.output_dir


class TestNativeVideo:
    def test_archived_bytes_are_written_and_linked(self, tmp_path):
        directory = run(
            tmp_path,
            PROSE + '<video src="https://video.example.hu/a.mp4"></video>',
            [{"uri": "https://video.example.hu/a.mp4",
              "content_type": "video/mp4", "body": MP4_BODY}])
        record = load(directory, "videos.json")[0]
        assert record["id"] == "video_001"
        assert record["archived"] is True
        assert record["local_file"] == "videos/video_001.mp4"
        assert (directory / record["local_file"]).is_file()

    def test_a_video_absent_from_the_capture_is_recorded_without_a_file(
            self, tmp_path):
        directory = run(
            tmp_path, PROSE + '<video src="https://video.example.hu/a.mp4"></video>')
        record = load(directory, "videos.json")[0]
        assert record["archived"] is False
        assert record["local_file"] is None


class TestEmbeds:
    def test_a_youtube_iframe_is_identified_but_never_re_embedded(self, tmp_path):
        directory = run(
            tmp_path,
            PROSE + '<iframe src="https://www.youtube.com/embed/UwTYPHnSP8M"></iframe>')
        record = load(directory, "videos.json")[0]
        assert record["platform"] == "youtube"
        assert record["external_id"] == "UwTYPHnSP8M"
        assert record["url"] == "https://www.youtube.com/watch?v=UwTYPHnSP8M"
        assert record["thumbnail_url"].startswith("https://i.ytimg.com/")
        html = (directory / "readability.html").read_text(encoding="utf-8")
        assert "<iframe" not in html

    def test_a_tag_manager_frame_is_not_a_video(self, tmp_path):
        directory = run(
            tmp_path,
            PROSE + '<iframe src="https://www.googletagmanager.com/ns.html?id=GTM-1">'
            '</iframe>')
        assert load(directory, "videos.json") == []


class TestHls:
    def test_a_segment_set_is_never_written_as_a_playable_file(self, tmp_path):
        # The write is refused anyway (no ffmpeg), but a wrong "complete" flag
        # would make a later backfill skip exactly the videos it must fetch.
        directory = run(
            tmp_path,
            PROSE + '<video src="https://stream.example.hu/chunklist.m3u8"></video>',
            [{"uri": "https://stream.example.hu/media_0.ts",
              "content_type": "video/mp2t", "body": MP4_BODY}])
        for record in load(directory, "videos.json"):
            assert record["local_file"] is None
        assert not (directory / "videos").exists() or \
            not list((directory / "videos").iterdir())


class TestRemovedFields:
    def test_the_debug_fields_are_gone(self, tmp_path):
        directory = run(
            tmp_path,
            PROSE + '<iframe src="https://www.youtube.com/embed/abc"></iframe>')
        record = load(directory, "videos.json")[0]
        for absent in ("position", "block_id", "discovered_via", "capture_urls",
                       "capture_bytes", "capture_complete"):
            assert absent not in record

    def test_the_key_set_is_exactly_the_contract(self, tmp_path):
        directory = run(
            tmp_path,
            PROSE + '<iframe src="https://www.youtube.com/embed/abc"></iframe>')
        assert set(load(directory, "videos.json")[0]) == {
            "id", "type", "platform", "external_id", "url", "embed_url",
            "thumbnail_url", "title", "caption", "local_file", "archived"}


class TestLinkToContent:
    def test_every_video_block_resolves_to_a_record(self, tmp_path):
        directory = run(
            tmp_path,
            PROSE + '<iframe src="https://www.youtube.com/embed/abc"></iframe>')
        ids = {r["id"] for r in load(directory, "videos.json")}
        blocks = [b for b in load(directory, "content.json")["blocks"]
                  if b["type"] == "video"]
        assert blocks
        for block in blocks:
            assert block["video_id"] in ids


def fbcdn(video_id, tag, bitrate=1000):
    """A Facebook media URL carrying the metadata Facebook really puts there."""
    import base64, json
    blob = json.dumps({"vencode_tag": tag, "video_id": video_id,
                       "duration_s": 60, "bitrate": bitrate})
    efg = base64.b64encode(blob.encode()).decode()
    return ("https://video-vie1-1.xx.fbcdn.net/o1/v/t2/f2/m366/"
            "AQ%s.mp4?_nc_cat=107&efg=%s" % (str(video_id)[-6:], efg))


REEL_A = 1699575194535875
REEL_B = 678407308664933


def fb_embed(reel):
    return ('<iframe src="https://www.facebook.com/plugins/video.php'
            '?height=476&href=https%%3A%%2F%%2Fwww.facebook.com%%2Freel%%2F'
            '%d%%2F&show_text=false"></iframe>' % reel)


class TestPayloadIdentity:
    """A CDN payload's own metadata, which is how three reels are told apart."""

    def test_a_facebook_url_states_its_video_id_and_encode_tag(self):
        from causalia_extractor.videos import payload_identity
        assert payload_identity(fbcdn(REEL_A, "dash_r2av1-r1gen2vp9-m3_q80")) == \
            (str(REEL_A), "dash_r2av1-r1gen2vp9-m3_q80")

    def test_a_url_with_no_metadata_yields_nothing(self):
        from causalia_extractor.videos import payload_identity
        assert payload_identity("https://cdn.example.com/a.mp4") == (None, None)

    def test_a_corrupt_parameter_does_not_raise(self):
        from causalia_extractor.videos import payload_identity
        assert payload_identity(
            "https://video.xx.fbcdn.net/a.mp4?efg=!!!not-base64!!!") == (None, None)

    def test_a_dash_rung_is_recognised_as_adaptive(self):
        from causalia_extractor.videos import is_adaptive_rendition, is_progressive
        assert is_adaptive_rendition("dash_r2av1-r1gen2vp9-m3_q80")
        assert is_adaptive_rendition("dash_ln_heaac_vbr3_audio")
        assert not is_progressive("dash_r2av1-r1gen2vp9-m3_q80")

    def test_a_progressive_stream_is_not_adaptive(self):
        from causalia_extractor.videos import is_adaptive_rendition, is_progressive
        assert is_progressive("xpv_progressive.FACEBOOK..C3.360.sve_sd")
        assert not is_adaptive_rendition("xpv_progressive.FACEBOOK..C3.360.sve_sd")

    def test_an_untagged_payload_is_not_adaptive(self):
        from causalia_extractor.videos import is_adaptive_rendition
        assert not is_adaptive_rendition(None)
        assert not is_adaptive_rendition("")


class TestAdaptiveLadders:
    """Nine rungs of one video are not nine videos.

    Measured on mandiner.hu f300764f: three facebook reels, 19 captured
    payloads. videos.json claimed 22 videos and 92 MB was written as 19 files,
    none of which is playable - a DASH video rung is silent and the audio is a
    separate file.
    """

    LADDER_A = [{"uri": fbcdn(REEL_A, "dash_r2av1-r1gen2vp9-m3_q%d0" % q,
                              bitrate=q * 100000),
                 "content_type": "video/mp4",
                 "body": MP4_BODY + bytes(q * 100)} for q in (2, 5, 9)]
    LADDER_B = [{"uri": fbcdn(REEL_B, "dash_r2av1-r1gen2vp9-m3_q%d0" % q,
                              bitrate=q * 100000),
                 "content_type": "video/mp4",
                 "body": MP4_BODY + bytes(q * 50)} for q in (2, 9)]

    def test_a_ladder_is_attributed_to_its_own_player(self, tmp_path):
        directory = run(tmp_path, PROSE + fb_embed(REEL_A), self.LADDER_A)
        records = load(directory, "videos.json")
        assert len(records) == 1
        assert str(REEL_A) in records[0]["external_id"]

    def test_two_players_on_one_page_are_told_apart(self, tmp_path):
        # This is what the CDN host alone cannot do: two facebook embeds and
        # five fbcdn payloads on one host.
        directory = run(tmp_path, PROSE + fb_embed(REEL_A) + fb_embed(REEL_B),
                        self.LADDER_A + self.LADDER_B)
        records = load(directory, "videos.json")
        assert len(records) == 2
        ids = {r["external_id"] for r in records}
        assert any(str(REEL_A) in i for i in ids)
        assert any(str(REEL_B) in i for i in ids)

    def test_no_rung_is_written_as_a_playable_file(self, tmp_path):
        directory = run(tmp_path, PROSE + fb_embed(REEL_A), self.LADDER_A)
        assert load(directory, "videos.json")[0]["local_file"] is None
        assert not (directory / "videos").exists() or \
            not list((directory / "videos").iterdir())

    def test_the_bytes_are_still_reported_as_archived(self, tmp_path):
        # They really are in the WACZ. `archived` says so; `local_file` says
        # nothing playable came out of them.
        directory = run(tmp_path, PROSE + fb_embed(REEL_A), self.LADDER_A)
        assert load(directory, "videos.json")[0]["archived"] is True

    def test_an_unattributable_rung_is_not_recorded_as_a_video(self, tmp_path):
        # A ladder for a reel that is not embedded on this page. The bytes stay
        # in the WACZ; inventing a video record for a silent fragment would put
        # nine phantom rows in videos.json.
        directory = run(tmp_path, PROSE + fb_embed(REEL_A), self.LADDER_B)
        records = load(directory, "videos.json")
        assert len(records) == 1
        assert str(REEL_A) in records[0]["external_id"]

    def test_a_lone_untagged_payload_is_attributed_by_its_cdn_host(self, tmp_path):
        # One embed on the platform, one payload from its CDN: not ambiguous,
        # and the older host rule is still the right answer.
        progressive = [{"uri": fbcdn(0, "xpv_progressive.FACEBOOK..C3.360.sve_sd"),
                        "content_type": "video/mp4", "body": MP4_BODY}]
        directory = run(tmp_path, PROSE + fb_embed(REEL_A), progressive)
        records = load(directory, "videos.json")
        assert len(records) == 1
        assert records[0]["local_file"] == "videos/video_001.mp4"

    def test_an_unattributable_progressive_stream_still_gets_a_record(self, tmp_path):
        # Two embeds, so the host rule cannot decide, and the stream names no
        # video_id. It is self-contained and playable, so bytes we hold must
        # appear in videos.json - this is the real f300764f case.
        progressive = [{"uri": fbcdn(0, "xpv_progressive.FACEBOOK..C3.360.sve_sd"),
                        "content_type": "video/mp4", "body": MP4_BODY}]
        directory = run(tmp_path, PROSE + fb_embed(REEL_A) + fb_embed(REEL_B),
                        progressive)
        records = load(directory, "videos.json")
        assert len(records) == 3
        loose = [r for r in records if not r["external_id"]]
        assert len(loose) == 1 and loose[0]["local_file"]

    def test_a_progressive_stream_beats_a_bigger_rung(self, tmp_path):
        # Picking purely by size selects the q90 DASH track - the biggest file
        # and a silent one.
        payloads = self.LADDER_A + [
            {"uri": fbcdn(REEL_A, "xpv_progressive.FACEBOOK..C3.360.sve_sd"),
             "content_type": "video/mp4", "body": MP4_BODY}]
        directory = run(tmp_path, PROSE + fb_embed(REEL_A), payloads)
        record = load(directory, "videos.json")[0]
        assert record["local_file"] == "videos/video_001.mp4"

    def test_an_ordinary_self_hosted_video_is_unaffected(self, tmp_path):
        # No CDN metadata, no ladder: the plain path must keep working.
        directory = run(
            tmp_path,
            PROSE + '<video src="https://video.example.hu/a.mp4"></video>',
            [{"uri": "https://video.example.hu/a.mp4",
              "content_type": "video/mp4", "body": MP4_BODY}])
        record = load(directory, "videos.json")[0]
        assert record["archived"] is True
        assert record["local_file"] == "videos/video_001.mp4"
        assert (directory / record["local_file"]).is_file()
