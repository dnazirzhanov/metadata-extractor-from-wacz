"""Lifecycle, isolation and the command line."""

import json
import socket

import pytest

from causalia_extractor.cli import main
from causalia_extractor.identity import (
    ArticleLocation, archive_id_for, iter_wacz_files, outlet_of)
from causalia_extractor.output import ArchiveFingerprint, UnsafeArtifact, _check_artifact_name
from causalia_extractor.pipeline import extract
from conftest import (ARTICLE_BODY, ARTICLE_URL, JPEG_IMAGE, PNG_IMAGE,
                      html_document, load, make_wacz)


def simple_wacz(tmp_path, name="page.wacz", url=ARTICLE_URL, body=None):
    return make_wacz(tmp_path / name, page_url=url, records=[
        {"uri": url, "content_type": "text/html",
         "body": html_document(body or ARTICLE_BODY)},
        {"uri": "https://cdn.example.com/kep.jpg",
         "content_type": "image/jpeg", "body": JPEG_IMAGE},
        {"uri": f"urn:fullPage:{url}", "type": "resource",
         "content_type": "image/png", "body": PNG_IMAGE}])


class TestOutputSet:
    def test_the_expected_artifacts_are_produced(self, tmp_path):
        directory = extract(simple_wacz(tmp_path), tmp_path / "out").output_dir
        produced = {p.name for p in directory.iterdir()}
        assert {"original.html", "readability.html", "article.json",
                "content.json", "images.json", "videos.json", "links.json",
                "extraction.json", "screenshot.png", "images"} <= produced

    def test_no_debug_artifact_is_left_behind(self, tmp_path):
        directory = extract(simple_wacz(tmp_path), tmp_path / "out").output_dir
        names = {p.name for p in directory.rglob("*") if p.is_file()}
        assert not any(n.endswith((".part", ".tmp", ".bak")) for n in names)
        assert "thumb.webp" not in names


class TestLifecycle:
    def test_extraction_json_holds_exactly_three_fields(self, tmp_path):
        directory = extract(simple_wacz(tmp_path), tmp_path / "out").output_dir
        payload = load(directory, "extraction.json")
        assert set(payload) == {"extraction_version", "extracted_at",
                                "extraction_status"}

    def test_none_of_the_old_diagnostics_are_persisted(self, tmp_path):
        directory = extract(simple_wacz(tmp_path), tmp_path / "out").output_dir
        text = (directory / "extraction.json").read_text(encoding="utf-8")
        for absent in ("size_bytes", "mtime_ns", "sha256", "results",
                       "warnings", "notes", "timings_ms"):
            assert absent not in text

    def test_a_good_article_is_success(self, tmp_path):
        assert extract(simple_wacz(tmp_path), tmp_path / "out").status == "success"

    def test_an_article_with_no_blocks_is_partial_not_failed(self, tmp_path):
        wacz = make_wacz(tmp_path / "page.wacz", records=[
            {"uri": ARTICLE_URL, "content_type": "text/html",
             "body": html_document("<div></div>")}])
        result = extract(wacz, tmp_path / "out")
        assert result.status == "partial"
        assert any("no article content blocks" in w for w in result.warnings)

    def test_an_unreadable_archive_is_failed_and_reported(self, tmp_path):
        broken = tmp_path / "broken.wacz"
        broken.write_bytes(b"not a zip at all")
        result = extract(broken, tmp_path / "out")
        assert result.status == "failed"
        assert result.error

    def test_a_failure_is_never_silently_discarded(self, tmp_path):
        broken = tmp_path / "broken.wacz"
        broken.write_bytes(b"not a zip at all")
        assert extract(broken, tmp_path / "out").error is not None


class TestIsolation:
    def test_the_archive_is_not_modified(self, tmp_path):
        wacz = simple_wacz(tmp_path)
        before = ArchiveFingerprint.of(wacz)
        extract(wacz, tmp_path / "out")
        assert ArchiveFingerprint.of(wacz) == before

    def test_the_archive_is_never_deleted(self, tmp_path):
        wacz = simple_wacz(tmp_path)
        extract(wacz, tmp_path / "out")
        assert wacz.is_file()

    def test_nothing_is_written_beside_the_archive(self, tmp_path):
        source = tmp_path / "corpus"
        source.mkdir()
        wacz = simple_wacz(source)
        before = {p.name for p in source.iterdir()}
        extract(wacz, tmp_path / "out")
        assert {p.name for p in source.iterdir()} == before

    def test_no_socket_is_opened_during_an_extraction(self, tmp_path, monkeypatch):
        def refuse(*args, **kwargs):
            raise AssertionError("the extractor must never open a socket")
        monkeypatch.setattr(socket, "socket", refuse)
        monkeypatch.setattr(socket, "create_connection", refuse)
        assert extract(simple_wacz(tmp_path), tmp_path / "out").ok

    def test_the_archive_itself_can_never_be_written_as_an_artifact(self):
        for name in ("page.wacz", "page.warc.gz", "datapackage.json",
                     "../escape.json", "/etc/passwd"):
            with pytest.raises(UnsafeArtifact):
                _check_artifact_name(name)

    def test_the_wacz_is_not_copied_unless_asked(self, tmp_path):
        directory = extract(simple_wacz(tmp_path), tmp_path / "out").output_dir
        assert not (directory / "page.wacz").exists()

    def test_copy_wacz_opts_in(self, tmp_path):
        wacz = simple_wacz(tmp_path)
        directory = extract(wacz, tmp_path / "out", copy_wacz=True).output_dir
        assert (directory / "page.wacz").read_bytes() == wacz.read_bytes()


class TestDryRun:
    def test_it_writes_nothing_but_still_runs_the_pipeline(self, tmp_path):
        result = extract(simple_wacz(tmp_path), tmp_path / "out", dry_run=True)
        assert result.ok
        assert result.counts["blocks"] > 0
        assert not (tmp_path / "out").exists() or \
            not list((tmp_path / "out").rglob("*.json"))


class TestIdentity:
    def test_identity_comes_from_the_corpus_path_when_it_is_there(self, tmp_path):
        wacz = simple_wacz(tmp_path / "ripost.hu" / "aa" / ("a" * 64))
        location = ArticleLocation.from_wacz(wacz)
        assert location.archive_id == "a" * 64
        assert location.outlet == "ripost.hu"
        assert location.from_path

    def test_identity_is_derived_from_the_page_url_when_it_is_not(self, tmp_path):
        directory = extract(simple_wacz(tmp_path, name="pilot.wacz"),
                            tmp_path / "out").output_dir
        article = load(directory, "article.json")
        assert article["archive_id"] == archive_id_for(ARTICLE_URL)
        assert article["outlet"] == "ripost.hu"

    def test_the_derivation_matches_the_archivers_own_hash(self):
        # Verified against the live corpus: these two directories exist on
        # milab2 under exactly these names.
        assert archive_id_for(
            "https://ripost.hu/sztardzsusz/2020/10/"
            "kegyetlenul-meggyilkoltak-otthonaban-a-hires-szineszt"
        ) == "00003bdc9017bf448e12558c3b1848abb6b894c5bf733a07f609161ce5f47095"
        assert archive_id_for(
            "https://magyarnemzet.hu/belfold/2025/12/"
            "lengyel-laszlo-tisza-megszoritas-adoemeles"
        ) == "58b97ac53200b3966404e19a418874b06c811bbd2101a62eb5e625138e31e981"

    def test_tracking_parameters_do_not_change_identity(self):
        assert archive_id_for("https://ripost.hu/a/b?utm_source=fb") == \
            archive_id_for("https://ripost.hu/a/b")

    def test_subdomains_collapse_to_the_registrable_domain(self):
        assert outlet_of("https://www.bama.hu/x") == "bama.hu"
        assert outlet_of("https://sport.origo.hu/x") == "origo.hu"


class TestDiscovery:
    def test_a_single_archive_is_found(self, tmp_path):
        wacz = simple_wacz(tmp_path)
        assert list(iter_wacz_files(wacz)) == [wacz]

    def test_a_tree_is_walked_in_a_stable_order(self, tmp_path):
        for outlet in ("ripost.hu", "bama.hu"):
            for shard in ("aa", "bb"):
                simple_wacz(tmp_path / outlet / shard / (shard * 32))
        found = [str(p) for p in iter_wacz_files(tmp_path)]
        assert found == sorted(found)
        assert len(found) == 4

    def test_an_outlet_filter_restricts_the_walk(self, tmp_path):
        for outlet in ("ripost.hu", "bama.hu"):
            simple_wacz(tmp_path / outlet / "aa" / ("a" * 64))
        found = list(iter_wacz_files(tmp_path, outlet="bama.hu"))
        assert len(found) == 1 and "bama.hu" in str(found[0])

    def test_an_archive_that_is_not_called_page_wacz_is_still_found(self, tmp_path):
        wacz = simple_wacz(tmp_path / "pilot", name="capture.wacz")
        assert list(iter_wacz_files(tmp_path / "pilot")) == [wacz]


class TestCli:
    def test_a_successful_run_exits_zero(self, tmp_path, capsys):
        simple_wacz(tmp_path / "in" / "ripost.hu" / "aa" / ("a" * 64))
        code = main(["extract", "--input", str(tmp_path / "in"),
                     "--output", str(tmp_path / "out"), "--log-level", "ERROR"])
        assert code == 0
        assert "success" in capsys.readouterr().out

    def test_a_failed_article_exits_one(self, tmp_path, capsys):
        broken = tmp_path / "in" / "broken.wacz"
        broken.parent.mkdir(parents=True)
        broken.write_bytes(b"not a zip")
        code = main(["extract", "--input", str(tmp_path / "in"),
                     "--output", str(tmp_path / "out"), "--log-level", "ERROR"])
        assert code == 1

    def test_an_empty_input_is_reported(self, tmp_path):
        (tmp_path / "in").mkdir()
        assert main(["extract", "--input", str(tmp_path / "in"),
                     "--output", str(tmp_path / "out"),
                     "--log-level", "ERROR"]) == 1

    def test_the_limit_is_honoured(self, tmp_path, capsys):
        for shard in ("aa", "bb", "cc"):
            simple_wacz(tmp_path / "in" / "ripost.hu" / shard / (shard * 32))
        main(["extract", "--input", str(tmp_path / "in"),
              "--output", str(tmp_path / "out"), "--limit", "2",
              "--log-level", "ERROR"])
        assert "2 archive(s) processed" in capsys.readouterr().out

    def test_the_output_path_is_required(self, tmp_path):
        with pytest.raises(SystemExit):
            main(["extract", "--input", str(tmp_path)])
