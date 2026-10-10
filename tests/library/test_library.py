"""Tests for backend/library.py (PDF 预览 + 教材下载).

Run from the repository root:
    python -m unittest discover -s tests/library -v
Uses a synthetic catalog release and tiny generated PDFs; no network, no models.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import library  # noqa: E402
import synthetic  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Base(unittest.TestCase):
    env_extra: dict = {}
    build_release = True

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sun-library-test-"))
        cls.data_root = cls.tmp / "data"
        cls.catalog_root = cls.data_root / "catalog"
        cls.pdf_dir = cls.tmp / "pdf"
        cls.catalog_root.mkdir(parents=True)
        cls.pdf_dir.mkdir()
        cls.info = synthetic.build(cls.catalog_root, cls.pdf_dir) if cls.build_release else None
        cls.clock = FakeClock()
        cls.app, cls.lib = cls.make_app(cls.env_extra, cls.clock)
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def make_app(cls, extra=None, clock=None, client=None):
        env = {"PDF_SERVE_MODE": "direct", "LIBRARY_PDF_ROOT": str(cls.pdf_dir)}
        env.update(extra or {})
        app = FastAPI()

        @app.get("/api/health")
        def health():
            return {"status": "ok"}

        lib = library.install(app, data_root=cls.data_root, frontend_dir=None, env=env, clock=clock or FakeClock())
        return app, lib

    def get(self, url, **kwargs):
        return self.client.get(url, **kwargs)

    def eid(self, name):
        return self.info[name]["edition_id"]

    def pdf_url(self, name, download=False):
        edition = self.info[name]
        url = f"/files/pdf/{edition['edition_id']}/{edition['pdf']['sha256'][:12]}/x.pdf"
        return url + ("?download=1" if download else "")

    def pdf_bytes(self, name):
        return (self.pdf_dir / f"{self.info[name]['pdf']['sha256']}.pdf").read_bytes()


# ── Release loading ──────────────────────────────────────────────────────────

class ReleaseLoadingTests(Base):
    def test_valid_release(self):
        body = self.get("/api/library/release").json()
        self.assertTrue(body["available"])
        self.assertEqual(body["release_id"], "r-synthetic-1")
        self.assertEqual(body["schema"], "sun-catalog-v1")
        self.assertEqual(body["counts"]["editions"], 6)
        self.assertEqual(body["counts"]["previewable"], 5)
        self.assertEqual(body["counts"]["active_previewable"], 4)
        self.assertEqual(body["counts"]["downloads"], 78)
        self.assertEqual(body["counts"]["page_maps"], 3)
        self.assertEqual(body["counts"]["dropped_sources"], 2)
        self.assertEqual(body["coverage"]["printed_verified_editions"], 1)
        self.assertEqual(body["stages"], ["小学", "初中", "高中"])
        self.assertEqual(body["regions"], ["CN"])
        self.assertNotIn(str(self.tmp), json.dumps(body))

    def test_every_response_names_the_same_release(self):
        for url in ("/api/library/editions", "/api/library/downloads", "/api/library/downloads/facets",
                    f"/api/library/editions/{self.eid('e1')}", f"/api/library/editions/{self.eid('e1')}/pages",
                    "/api/library/resolve?items=aaaaaaaaaaaa.1", self.pdf_url("e1")):
            response = self.get(url)
            self.assertEqual(response.headers["x-library-release"], "r-synthetic-1", url)

    def _state(self, catalog_root):
        settings = library.Settings.from_env(self.data_root, {"LIBRARY_CATALOG_ROOT": str(catalog_root)})
        return library.LibraryState(settings, FakeClock())

    def _copy_catalog(self):
        target = Path(tempfile.mkdtemp(prefix="cat-", dir=self.tmp)) / "catalog"
        shutil.copytree(self.catalog_root, target)
        return target

    def test_missing_current(self):
        state = self._state(self.tmp / "nowhere")
        self.assertIsNone(state.get())
        self.assertEqual(state.error, "no CURRENT pointer")

    def test_current_must_be_a_plain_release_id(self):
        for bad in ("../r-synthetic-1", "a/b", "", "r 1", "..", "x" * 80):
            root = self._copy_catalog()
            (root / "CURRENT").write_text(bad, encoding="utf-8")
            state = self._state(root)
            self.assertIsNone(state.get(), bad)
            self.assertIn("CURRENT", state.error)

    def test_current_pointing_to_missing_release(self):
        root = self._copy_catalog()
        (root / "CURRENT").write_text("r-does-not-exist\n", encoding="utf-8")
        state = self._state(root)
        self.assertIsNone(state.get())
        self.assertEqual(state.error, "release directory not found")

    def test_corrupt_json_is_rejected(self):
        root = self._copy_catalog()
        target = root / "releases" / "r-synthetic-1" / "catalog.json"
        data = b"{not json"
        target.write_bytes(data)
        sums = root / "releases" / "r-synthetic-1" / "SHA256SUMS"
        lines = [line for line in sums.read_text().splitlines() if not line.endswith("  catalog.json")]
        lines.append(f"{hashlib.sha256(data).hexdigest()}  catalog.json")
        sums.write_text("\n".join(lines) + "\n")
        state = self._state(root)
        self.assertIsNone(state.get())
        self.assertEqual(state.error, "invalid JSON: catalog.json")

    def test_sha_mismatch_is_rejected(self):
        root = self._copy_catalog()
        target = root / "releases" / "r-synthetic-1" / "downloads.json"
        target.write_bytes(target.read_bytes() + b"\n")
        state = self._state(root)
        self.assertIsNone(state.get())
        self.assertEqual(state.error, "sha256 mismatch: downloads.json")

    def test_page_map_tamper_is_rejected(self):
        root = self._copy_catalog()
        target = root / "releases" / "r-synthetic-1" / "page-maps" / f"{self.eid('e1')}.json"
        target.write_text(target.read_text().replace('"legacy_image_index": 3', '"legacy_image_index": 9'))
        state = self._state(root)
        self.assertIsNone(state.get())
        self.assertIn("sha256 mismatch", state.error)

    def test_missing_sums_file(self):
        root = self._copy_catalog()
        (root / "releases" / "r-synthetic-1" / "SHA256SUMS").unlink()
        state = self._state(root)
        self.assertIsNone(state.get())
        self.assertEqual(state.error, "missing file: SHA256SUMS")

    def test_unsafe_path_in_sums(self):
        root = self._copy_catalog()
        sums = root / "releases" / "r-synthetic-1" / "SHA256SUMS"
        sums.write_text(sums.read_text() + f"{'0' * 64}  ../../CURRENT\n")
        state = self._state(root)
        self.assertIsNone(state.get())
        self.assertEqual(state.error, "unsafe path in SHA256SUMS")

    def _rewrite(self, mutate, release_id="r-bad"):
        root = Path(tempfile.mkdtemp(prefix="cat-", dir=self.tmp)) / "catalog"
        editions = json.loads(json.dumps(self.info["editions"]))
        downloads = json.loads(json.dumps(self.info["downloads"]))
        page_maps = json.loads(json.dumps(self.info["page_maps"]))
        mutate(editions, downloads, page_maps)
        synthetic.write_release(root / "releases" / release_id, release_id, editions, downloads, page_maps)
        synthetic.set_current(root, release_id)
        return self._state(root)

    def test_shape_validation(self):
        cases = {
            "page map length does not equal PDF page count": lambda e, d, p: p[e[0]["edition_id"]]["pages"].pop(),
            "page map rows must be contiguous from 1": lambda e, d, p: p[e[0]["edition_id"]]["pages"][4].update(pdf_page=9),
            "duplicate legacy_image_index": lambda e, d, p: p[e[0]["edition_id"]]["pages"][4].update(legacy_image_index=0),
            "verified page without printed_label": lambda e, d, p: p[e[0]["edition_id"]]["pages"][0].update(printed_verified=True),
            "page map bound to another PDF": lambda e, d, p: p[e[0]["edition_id"]].update(pdf_sha256="f" * 64),
            "download row points to unknown edition": lambda e, d, p: d[0].update(edition_id="cn-nope-000000000000"),
            "invalid pdf.sha256": lambda e, d, p: e[1]["pdf"].update(sha256="../../etc/passwd"),
            "invalid edition_id": lambda e, d, p: e[1].update(edition_id="../x"),
            "invalid status": lambda e, d, p: e[1].update(status="live"),
            "invalid corpus.relation": lambda e, d, p: e[1]["corpus"].update(relation="same"),
            "duplicate edition_id": lambda e, d, p: e.append(dict(e[1])),
            "invalid pdf.pages": lambda e, d, p: e[1]["pdf"].update(pages=0),
        }
        for expected, mutate in cases.items():
            state = self._rewrite(mutate)
            self.assertIsNone(state.get(), expected)
            self.assertIn(expected, state.error)

    def test_release_id_must_match_directory(self):
        root = self._copy_catalog()
        shutil.copytree(root / "releases" / "r-synthetic-1", root / "releases" / "r-copy")
        (root / "CURRENT").write_text("r-copy\n")
        state = self._state(root)
        self.assertIsNone(state.get())
        self.assertEqual(state.error, "RELEASE.json release_id does not match directory")


class MissingCatalogTests(Base):
    build_release = False

    def test_tabs_report_unavailable_and_sun_keeps_working(self):
        with TestClient(self.app) as client:  # runs the startup hook; must not raise
            self.assertEqual(client.get("/api/health").json(), {"status": "ok"})
            body = client.get("/api/library/release").json()
            self.assertFalse(body["available"])
            self.assertEqual(body["message"], "目录尚未发布或不可用")
            for url in ("/api/library/editions", "/api/library/downloads", "/api/library/downloads/facets",
                        "/api/library/editions/cn-x-000000000000", "/api/library/resolve?book_key=a&section=1",
                        "/api/library/legacy?kind=jc&ref=a&page=1", "/files/pdf/cn-x-000000000000/000000000000/a.pdf"):
                response = client.get(url)
                self.assertEqual(response.status_code, 503, url)
                self.assertEqual(response.headers["retry-after"], "60")


class ReloadTests(Base):
    env_extra = {"LIBRARY_RELOAD_SECONDS": "60"}

    def test_switch_is_checked_at_most_once_per_interval_and_bad_release_keeps_old(self):
        self.assertEqual(self.get("/api/library/release").json()["release_id"], "r-synthetic-1")
        release_before = self.lib.state.get()

        # A complete second release, then an atomic pointer switch.
        editions = [e for e in self.info["editions"] if e["edition_id"] != self.eid("e2")]
        downloads = [d for d in self.info["downloads"] if d["edition_id"] != self.eid("e2")]
        synthetic.write_release(self.catalog_root / "releases" / "r-synthetic-2", "r-synthetic-2", editions, downloads,
                                self.info["page_maps"])
        synthetic.set_current(self.catalog_root, "r-synthetic-2")

        self.clock.advance(30)
        self.assertIs(self.lib.state.get(), release_before)  # not re-checked yet
        self.assertEqual(self.get(f"/api/library/editions/{self.eid('e2')}").status_code, 200)

        self.clock.advance(31)
        body = self.get("/api/library/release").json()
        self.assertEqual(body["release_id"], "r-synthetic-2")
        self.assertEqual(self.get(f"/api/library/editions/{self.eid('e2')}").status_code, 404)
        self.assertEqual(self.get(self.pdf_url("e2")).status_code, 404)  # all consumers switched together
        release_two = self.lib.state.get()

        # A broken third release must not replace the good one.
        bad = self.catalog_root / "releases" / "r-broken"
        shutil.copytree(self.catalog_root / "releases" / "r-synthetic-2", bad)
        synthetic.set_current(self.catalog_root, "r-broken")
        self.clock.advance(61)
        body = self.get("/api/library/release").json()
        self.assertEqual(body["release_id"], "r-synthetic-2")
        self.assertEqual(body["pending_error"], "RELEASE.json release_id does not match directory")
        self.assertIs(self.lib.state.get(), release_two)

        # Rollback = point CURRENT back.
        synthetic.set_current(self.catalog_root, "r-synthetic-1")
        self.clock.advance(61)
        body = self.get("/api/library/release").json()
        self.assertEqual(body["release_id"], "r-synthetic-1")
        self.assertIsNone(body["pending_error"])


# ── Editions ─────────────────────────────────────────────────────────────────

class EditionTests(Base):
    def test_grid_lists_only_active_locally_stored_editions(self):
        body = self.get("/api/library/editions").json()
        ids = [e["edition_id"] for e in body["editions"]]
        self.assertEqual(body["total"], 4)
        self.assertIn(self.eid("e1"), ids)
        self.assertNotIn(self.eid("e4"), ids)  # superseded
        self.assertNotIn(self.eid("e5"), ids)  # no local file

    def test_phase_subject_and_title_filters(self):
        body = self.get("/api/library/editions", params={"phase": "高中"}).json()
        self.assertEqual({e["edition_id"] for e in body["editions"]}, {self.eid("e1"), self.eid("e2")})
        self.assertEqual({s["name"]: s["count"] for s in body["subjects"]}, {"语文": 1, "数学": 1})
        body = self.get("/api/library/editions", params={"phase": "高中", "subject": "数学"}).json()
        self.assertEqual([e["edition_id"] for e in body["editions"]], [self.eid("e2")])
        body = self.get("/api/library/editions", params={"phase": "初中", "q": "物理 八年级"}).json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(self.get("/api/library/editions", params={"q": "不存在的书"}).json()["total"], 0)
        self.assertEqual(self.get("/api/library/editions", params={"region": "TW"}).json()["total"], 0)

    def test_badges_are_taken_from_the_catalog(self):
        cards = {e["edition_id"]: e for e in self.get("/api/library/editions").json()["editions"]}
        e1, e2, e3 = cards[self.eid("e1")], cards[self.eid("e2")], cards[self.eid("e3")]
        self.assertEqual(e1["badges"], {"searchable": True, "corpus_relation": "aligned", "printed_verified": True,
                                        "has_text_layer": True, "text_identity": "page-aligned-other-printing"})
        self.assertEqual(e2["badges"], {"searchable": False, "corpus_relation": "none", "printed_verified": False,
                                        "has_text_layer": False, "text_identity": None})
        self.assertFalse(e3["badges"]["printed_verified"])
        self.assertEqual(e1["pages"], 12)

    def test_parameter_caps(self):
        for params in ({"limit": "61"}, {"limit": "0"}, {"limit": "abc"}, {"limit": "-1"}, {"offset": "-1"},
                       {"offset": "100001"}, {"offset": "1e3"}, {"q": "x" * 81}, {"subject": "x" * 61},
                       {"phase": "x" * 61}, {"region": "x" * 9}):
            self.assertEqual(self.get("/api/library/editions", params=params).status_code, 400, params)
        self.assertEqual(self.get("/api/library/editions", params={"limit": "60", "offset": "100000"}).status_code, 200)
        body = self.get("/api/library/editions", params={"limit": "1", "offset": "1"}).json()
        self.assertEqual((len(body["editions"]), body["total"]), (1, 4))

    def test_detail(self):
        body = self.get(f"/api/library/editions/{self.eid('e1')}").json()["edition"]
        self.assertTrue(body["previewable"])
        self.assertTrue(body["searchable"])
        sha12 = self.info["e1"]["pdf"]["sha256"][:12]
        self.assertTrue(body["file"]["url"].startswith(f"/files/pdf/{self.eid('e1')}/{sha12}/"))
        self.assertTrue(body["file"]["download_url"].endswith("?download=1"))
        self.assertEqual(body["page_map"], {"available": True, "version": "synthetic-1", "pages": 12,
                                            "printed_verified_pages": 10, "legacy_mapped_pages": 12})
        self.assertEqual([s["kind"] for s in body["sources"]], ["upstream"])
        # the other edition of the same work is listed separately with its own label
        self.assertEqual([(o["edition_id"], o["edition_label"], o["status"]) for o in body["other_editions"]],
                         [(self.eid("e4"), "样例版 2019", "superseded")])
        self.assertNotIn("path", json.dumps(body))
        self.assertNotIn("ti_md5", json.dumps(body))

    def test_detail_of_edition_without_local_file(self):
        body = self.get(f"/api/library/editions/{self.eid('e5')}").json()["edition"]
        self.assertFalse(body["previewable"])
        self.assertIsNone(body["file"])
        self.assertEqual(body["sources"][0]["kind"], "drive")

    def test_unknown_or_malformed_edition(self):
        for edition_id in ("cn-nope-000000000000", "..", "A", "cn%2F..%2Fx", "x" * 200):
            self.assertEqual(self.get(f"/api/library/editions/{edition_id}").status_code, 404, edition_id)


class PageTests(Base):
    def test_rows_and_printed_rule(self):
        body = self.get(f"/api/library/editions/{self.eid('e1')}/pages").json()
        self.assertEqual((body["from"], body["to"], body["pages"]), (1, 12, 12))
        self.assertEqual(body["rows"][0], {"pdf_page": 1, "printed_label": None, "printed_verified": False,
                                           "legacy_image_index": 0, "ocr_page": 1})
        self.assertEqual(body["rows"][2]["printed_label"], "1")
        self.assertTrue(body["rows"][2]["printed_verified"])

    def test_unverified_printed_labels_are_never_exposed(self):
        body = self.get(f"/api/library/editions/{self.eid('e3')}/pages").json()
        self.assertTrue(all(row["printed_label"] is None and row["printed_verified"] is False for row in body["rows"]))
        self.assertEqual([row["legacy_image_index"] for row in body["rows"]], [None, 1, 2, 5, None, None])

    def test_edition_without_page_map(self):
        body = self.get(f"/api/library/editions/{self.eid('e2')}/pages", params={"from": "2", "to": "3"}).json()
        self.assertFalse(body["page_map_available"])
        self.assertEqual(body["rows"], [
            {"pdf_page": 2, "printed_label": None, "printed_verified": False, "legacy_image_index": None, "ocr_page": None},
            {"pdf_page": 3, "printed_label": None, "printed_verified": False, "legacy_image_index": None, "ocr_page": None}])

    def test_caps(self):
        url = f"/api/library/editions/{self.eid('e1')}/pages"
        self.assertEqual(self.get(url, params={"from": "1", "to": "201"}).status_code, 400)
        self.assertEqual(self.get(url, params={"from": "5", "to": "4"}).status_code, 400)
        self.assertEqual(self.get(url, params={"from": "0"}).status_code, 400)
        self.assertEqual(self.get(url, params={"from": "x"}).status_code, 400)
        self.assertEqual(self.get(url, params={"from": "13"}).status_code, 404)
        body = self.get(url, params={"from": "10", "to": "200"}).json()
        self.assertEqual((body["from"], body["to"], len(body["rows"])), (10, 12, 3))
        self.assertEqual(self.get("/api/library/editions/cn-nope-000000000000/pages").status_code, 404)


# ── Resolve / legacy: never guess ────────────────────────────────────────────

class ResolveTests(Base):
    def test_single_exact(self):
        body = self.get("/api/library/resolve", params={"book_key": synthetic.REAL_SUN_KEY, "section": "8"}).json()
        self.assertEqual((body["available"], body["edition_id"], body["pdf_page"]), (True, self.eid("e1"), 9))

    def test_unmapped_page_is_not_guessed(self):
        body = self.get("/api/library/resolve", params={"book_key": synthetic.REAL_SUN_KEY, "section": "12"}).json()
        self.assertEqual(body, {"release_id": "r-synthetic-1", "available": False, "reason": "page-not-mapped"})
        # e3 maps only legacy 1, 2, 5: 3 and 4 lie between proven rows and still must not resolve
        for section in ("0", "3", "4"):
            body = self.get("/api/library/resolve", params={"book_key": synthetic.SECOND_SUN_KEY, "section": section}).json()
            self.assertFalse(body["available"])
            self.assertNotIn("pdf_page", body)

    def test_two_editions_claiming_the_same_page_are_ambiguous(self):
        body = self.get("/api/library/resolve", params={"book_key": synthetic.SECOND_SUN_KEY, "section": "2"}).json()
        self.assertEqual((body["available"], body["reason"]), (False, "ambiguous"))
        body = self.get("/api/library/resolve", params={"book_key": synthetic.SECOND_SUN_KEY, "section": "5"}).json()
        self.assertEqual((body["available"], body["edition_id"], body["pdf_page"]), (True, self.eid("e3"), 4))

    def test_unknown_book_and_title_is_not_an_identifier(self):
        body = self.get("/api/library/resolve", params={"book_key": "不存在", "section": "1"}).json()
        self.assertEqual((body["available"], body["reason"]), (False, "unknown-book"))
        body = self.get("/api/library/resolve", params={"book_key": self.info["e1"]["title"], "section": "1"}).json()
        self.assertFalse(body["available"])

    def test_single_caps(self):
        for params in ({}, {"book_key": "a"}, {"section": "1"}, {"book_key": "a" * 401, "section": "1"},
                       {"book_key": "a", "section": "-1"}, {"book_key": "a", "section": "1000001"},
                       {"book_key": "a", "section": "x"}):
            self.assertEqual(self.get("/api/library/resolve", params=params).status_code, 400, params)

    def test_batch(self):
        key = hashlib.md5(synthetic.REAL_SUN_KEY.encode()).hexdigest()[:12]
        other = hashlib.md5(synthetic.SECOND_SUN_KEY.encode()).hexdigest()[:12]
        body = self.get("/api/library/resolve", params={"items": f"{key}.0,{key}.99,{other}.2,ffffffffffff.3"}).json()
        self.assertEqual(body["results"], [
            {"item": f"{key}.0", "available": True, "edition_id": self.eid("e1"), "pdf_page": 1,
             "text_identity": "page-aligned-other-printing"},
            {"item": f"{key}.99", "available": False, "reason": "page-not-mapped"},
            {"item": f"{other}.2", "available": False, "reason": "ambiguous"},
            {"item": "ffffffffffff.3", "available": False, "reason": "unknown-book"}])

    def test_batch_caps(self):
        key = "a" * 12
        self.assertEqual(self.get("/api/library/resolve", params={"items": ",".join(f"{key}.{i}" for i in range(40))}).status_code, 200)
        self.assertEqual(self.get("/api/library/resolve", params={"items": ",".join(f"{key}.{i}" for i in range(41))}).status_code, 400)
        for items in ("", "abc", f"{key}", f"{key}.x", f"{key.upper()}.1", f"{key}.1;{key}.2", "x" * 2001):
            self.assertEqual(self.get("/api/library/resolve", params={"items": items}).status_code, 400, items)

    def test_in_book_sections(self):
        body = self.get("/api/library/resolve", params={"edition_id": self.eid("e3"), "sections": "1,3,5"}).json()
        self.assertEqual(body["results"], [
            {"section": 1, "available": True, "pdf_page": 2},
            {"section": 3, "available": False, "pdf_page": None, "reason": "page-not-mapped"},
            {"section": 5, "available": True, "pdf_page": 4}])
        # an edition with no proven relation never maps
        body = self.get("/api/library/resolve", params={"edition_id": self.eid("e2"), "sections": "1"}).json()
        self.assertFalse(body["results"][0]["available"])
        self.assertEqual(self.get("/api/library/resolve", params={"edition_id": self.eid("e1")}).status_code, 400)
        self.assertEqual(self.get("/api/library/resolve", params={"edition_id": self.eid("e1"), "sections": "1,a"}).status_code, 400)
        self.assertEqual(self.get("/api/library/resolve", params={"edition_id": self.eid("e1"), "sections": ",".join(map(str, range(41)))}).status_code, 400)
        self.assertEqual(self.get("/api/library/resolve", params={"edition_id": "cn-nope-000000000000", "sections": "1"}).status_code, 404)


class LegacyTests(Base):
    def test_jc_exact(self):
        body = self.get("/api/library/legacy", params={"kind": "jc", "ref": synthetic.REAL_SUN_KEY, "page": "8"}).json()
        self.assertEqual((body["available"], body["exact"], body["edition_id"], body["pdf_page"]), (True, True, self.eid("e1"), 9))

    def test_jc_unproven_page_opens_first_page_with_flag(self):
        for page, reason in (("500", "page-not-mapped"), ("abc", "bad-page"), ("-3", "bad-page"), (None, "no-page")):
            params = {"kind": "jc", "ref": synthetic.REAL_SUN_KEY}
            if page is not None:
                params["page"] = page
            body = self.get("/api/library/legacy", params=params).json()
            self.assertEqual((body["available"], body["exact"], body["pdf_page"], body["reason"]), (True, False, 1, reason), page)

    def test_jc_ambiguous_book_without_proof(self):
        body = self.get("/api/library/legacy", params={"kind": "jc", "ref": synthetic.SECOND_SUN_KEY, "page": "0"}).json()
        self.assertEqual((body["available"], body["exact"], body["reason"]), (False, False, "ambiguous"))
        body = self.get("/api/library/legacy", params={"kind": "jc", "ref": synthetic.SECOND_SUN_KEY, "page": "1"}).json()
        self.assertEqual((body["exact"], body["edition_id"], body["pdf_page"]), (True, self.eid("e3"), 2))

    def test_jks(self):
        body = self.get("/api/library/legacy", params={"kind": "jks", "ref": "gzyw_bx_s", "page": "9"}).json()
        self.assertEqual((body["exact"], body["edition_id"], body["pdf_page"]), (True, self.eid("e1"), 9))
        body = self.get("/api/library/legacy", params={"kind": "jks", "ref": "gzyw_bx_s", "page": "13"}).json()
        self.assertEqual((body["available"], body["exact"], body["pdf_page"]), (True, False, 1))
        body = self.get("/api/library/legacy", params={"kind": "jks", "ref": "gzyw_bx_x", "page": "9"}).json()
        self.assertEqual((body["available"], body["reason"]), (False, "unknown-book"))

    def test_jks_page_is_not_assumed_equal_without_declaration(self):
        def mutate(editions, downloads, page_maps):
            del editions[0]["legacy"]["jks_pages"]
        root = Path(tempfile.mkdtemp(prefix="cat-", dir=self.tmp))
        editions = json.loads(json.dumps(self.info["editions"]))
        mutate(editions, None, None)
        synthetic.write_release(root / "catalog" / "releases" / "r-nojks", "r-nojks", editions, self.info["downloads"], self.info["page_maps"])
        synthetic.set_current(root / "catalog", "r-nojks")
        app, _ = self.make_app({"LIBRARY_CATALOG_ROOT": str(root / "catalog")})
        body = TestClient(app).get("/api/library/legacy", params={"kind": "jks", "ref": "gzyw_bx_s", "page": "9"}).json()
        self.assertEqual((body["available"], body["exact"], body["pdf_page"], body["reason"]), (True, False, 1, "page-not-mapped"))

    def test_caps(self):
        for params in ({}, {"kind": "x", "ref": "a"}, {"kind": "jc"}, {"kind": "jc", "ref": "a" * 401},
                       {"kind": "jks", "ref": "../etc"}, {"kind": "jks", "ref": "a b"}):
            self.assertEqual(self.get("/api/library/legacy", params=params).status_code, 400, params)


# ── Downloads ────────────────────────────────────────────────────────────────

class DownloadTests(Base):
    def test_default_page_and_pagination(self):
        body = self.get("/api/library/downloads").json()
        self.assertEqual((body["total"], body["limit"], len(body["downloads"])), (78, 30, 30))
        self.assertEqual(body["downloads"][0]["stage"], "小学")  # all stages present, incl. 小学
        seen = []
        for offset in (0, 30, 60, 90):
            page = self.get("/api/library/downloads", params={"offset": str(offset), "limit": "30"}).json()
            seen += [row["source_id"] for row in page["downloads"]]
        self.assertEqual(len(seen), 78)
        self.assertEqual(len(set(seen)), 78)

    def test_caps(self):
        for params in ({"limit": "61"}, {"limit": "0"}, {"offset": "x"}, {"q": "x" * 81}, {"stage": "x" * 61},
                       {"publisher": "x" * 61}, {"subject": "x" * 61}):
            self.assertEqual(self.get("/api/library/downloads", params=params).status_code, 400, params)
        self.assertEqual(len(self.get("/api/library/downloads", params={"limit": "60"}).json()["downloads"]), 60)

    def test_filters(self):
        body = self.get("/api/library/downloads", params={"stage": "高中", "subject": "语文"}).json()
        self.assertEqual(body["total"], 3)
        body = self.get("/api/library/downloads", params={"stage": "小学", "publisher": "人民教育出版社"}).json()
        self.assertEqual(body["total"], 17)
        body = self.get("/api/library/downloads", params={"q": "第 07 册"}).json()
        self.assertEqual([row["source_id"] for row in body["downloads"]], ["gen-006"])

    def test_sources_are_ordered_and_derived_server_side(self):
        rows = {row["source_id"]: row for row in self.get("/api/library/downloads", params={"stage": "高中", "limit": "60"}).json()["downloads"]}
        e1 = rows[self.info["e1"]["source"]["source_id"]]
        self.assertEqual([s["kind"] for s in e1["sources"]], ["vps", "upstream"])
        self.assertEqual([s["label"] for s in e1["sources"]], ["本站", "官方平台"])
        sha12 = self.info["e1"]["pdf"]["sha256"][:12]
        self.assertTrue(e1["sources"][0]["url"].startswith(f"/files/pdf/{self.eid('e1')}/{sha12}/"))
        self.assertTrue(e1["sources"][0]["url"].endswith("?download=1"))
        self.assertTrue(e1["sources"][0]["same_origin"])
        self.assertFalse(e1["sources"][1]["same_origin"])
        self.assertTrue(e1["previewable"])
        self.assertEqual(e1["size_bytes"], self.info["e1"]["pdf"]["bytes"])
        self.assertTrue(e1["cover"].startswith("https://r1-ndr.ykt.cbern.com.cn/"))
        e5 = rows[self.info["e5"]["source"]["source_id"]]
        self.assertEqual([s["kind"] for s in e5["sources"]], ["drive", "upstream"])
        self.assertFalse(e5["previewable"])
        self.assertTrue(e5["sources"][0]["url"].startswith("https://drive.google.com/"))

    def test_untrusted_urls_are_dropped(self):
        rows = {row["source_id"]: row for row in self.get("/api/library/downloads", params={"stage": "高中", "limit": "60"}).json()["downloads"]}
        self.assertEqual(rows["bad-host-1"]["sources"], [])
        self.assertIsNone(rows["bad-host-1"]["cover"])
        self.assertEqual(rows["no-source-1"]["sources"], [])

    def test_editions_of_one_work_stay_distinct_rows(self):
        body = self.get("/api/library/downloads", params={"q": "语文 必修 上册"}).json()
        self.assertEqual(sorted((row["edition_id"], row["edition_label"]) for row in body["downloads"]),
                         sorted([(self.eid("e1"), "样例版 2026"), (self.eid("e4"), "样例版 2019")]))

    def test_facets(self):
        body = self.get("/api/library/downloads/facets").json()
        self.assertEqual([(s["name"], s["count"]) for s in body["stages"]], [("小学", 50), ("初中", 22), ("高中", 6)])
        self.assertEqual(body["total"], 78)
        body = self.get("/api/library/downloads/facets", params={"stage": "高中"}).json()
        self.assertEqual({s["name"]: s["count"] for s in body["subjects"]}, {"语文": 3, "数学": 1, "英语": 1, "历史": 1})
        self.assertEqual(self.get("/api/library/downloads/facets", params={"stage": "x" * 61}).status_code, 400)

    def test_upstream_template_is_used_only_when_configured(self):
        root = Path(tempfile.mkdtemp(prefix="cat-", dir=self.tmp)) / "catalog"
        downloads = [{"source_id": "abc-1", "stage": "小学", "subject": "语文", "title": "模板来源", "edition_label": None,
                      "publisher": None, "cover": None, "size_bytes": None, "sources": ["upstream"], "edition_id": None}]
        synthetic.write_release(root / "releases" / "r-t", "r-t", [], downloads, {})
        synthetic.set_current(root, "r-t")
        app, _ = self.make_app({"LIBRARY_CATALOG_ROOT": str(root)})
        self.assertEqual(TestClient(app).get("/api/library/downloads").json()["downloads"][0]["sources"], [])
        app, _ = self.make_app({"LIBRARY_CATALOG_ROOT": str(root), "LIBRARY_UPSTREAM_TEMPLATE": "https://jks-ai.bdfz.net/pdf/{source_id}"})
        self.assertEqual(TestClient(app).get("/api/library/downloads").json()["downloads"][0]["sources"][0]["url"],
                         "https://jks-ai.bdfz.net/pdf/abc-1")


# ── PDF delivery ─────────────────────────────────────────────────────────────

class PdfDirectTests(Base):
    def test_full_body_and_headers(self):
        response = self.get(self.pdf_url("e1"))
        data = self.pdf_bytes("e1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, data)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertEqual(response.headers["content-length"], str(len(data)))
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.headers["etag"], f'"{self.info["e1"]["pdf"]["sha256"]}"')
        self.assertEqual(response.headers["cache-control"], "public, max-age=31536000, immutable")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertTrue(response.headers["content-disposition"].startswith("inline; filename*=UTF-8''"))
        self.assertNotIn("set-cookie", response.headers)

    def test_download_disposition_uses_server_side_name(self):
        edition = self.info["e1"]
        url = f"/files/pdf/{edition['edition_id']}/{edition['pdf']['sha256'][:12]}/{quote('evil\"name.exe')}?download=1"
        response = self.get(url)
        self.assertEqual(response.status_code, 200)
        expected = quote("【样例】普通高中教科书·语文 必修 上册（样例版 2026）.pdf", safe="")
        self.assertEqual(response.headers["content-disposition"], f"attachment; filename*=UTF-8''{expected}")
        self.assertNotIn("evil", response.headers["content-disposition"])
        self.assertIn("inline", self.get(self.pdf_url("e1") + "?download=0").headers["content-disposition"])

    def test_head(self):
        response = self.client.head(self.pdf_url("e1"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["content-length"], str(len(self.pdf_bytes("e1"))))
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        response = self.client.head(self.pdf_url("e1"), headers={"Range": "bytes=0-9"})
        self.assertEqual((response.status_code, response.headers["content-length"]), (206, "10"))

    def test_single_ranges(self):
        data = self.pdf_bytes("e1")
        size = len(data)
        cases = {
            "bytes=0-0": (0, 0), "bytes=0-99": (0, 99), "bytes=100-": (100, size - 1), "bytes=-50": (size - 50, size - 1),
            f"bytes={size - 1}-": (size - 1, size - 1), f"bytes=10-{size + 500}": (10, size - 1),
            f"bytes=-{size + 10}": (0, size - 1), "Bytes = 5 - 9".replace(" ", ""): (5, 9),
        }
        for header, (start, end) in cases.items():
            response = self.get(self.pdf_url("e1"), headers={"Range": header})
            self.assertEqual(response.status_code, 206, header)
            self.assertEqual(response.headers["content-range"], f"bytes {start}-{end}/{size}", header)
            self.assertEqual(response.headers["content-length"], str(end - start + 1), header)
            self.assertEqual(response.content, data[start:end + 1], header)
            self.assertEqual(response.headers["content-type"], "application/pdf")

    def test_unsatisfiable_ranges(self):
        size = len(self.pdf_bytes("e1"))
        for header in (f"bytes={size}-", f"bytes={size + 5}-{size + 9}", "bytes=9-5", "bytes=-0", "bytes=-", "bytes=a-b",
                       "bytes=1-2-3", "bytes=" + "9" * 30 + "-"):
            response = self.get(self.pdf_url("e1"), headers={"Range": header})
            self.assertEqual(response.status_code, 416, header)
            self.assertEqual(response.headers["content-range"], f"bytes */{size}", header)

    def test_multi_range_and_foreign_units_fall_back_to_full_body(self):
        for header in ("bytes=0-1,5-6", "pages=1-2"):
            response = self.get(self.pdf_url("e1"), headers={"Range": header})
            self.assertEqual(response.status_code, 200, header)
            self.assertEqual(len(response.content), len(self.pdf_bytes("e1")))

    def test_if_range_and_if_none_match(self):
        etag = f'"{self.info["e1"]["pdf"]["sha256"]}"'
        self.assertEqual(self.get(self.pdf_url("e1"), headers={"Range": "bytes=0-9"}).status_code, 206)  # If-Range not required
        self.assertEqual(self.get(self.pdf_url("e1"), headers={"Range": "bytes=0-9", "If-Range": etag}).status_code, 206)
        self.assertEqual(self.get(self.pdf_url("e1"), headers={"Range": "bytes=0-9", "If-Range": '"other"'}).status_code, 200)
        self.assertEqual(self.get(self.pdf_url("e1"), headers={"If-None-Match": etag}).status_code, 304)

    def test_allow_list(self):
        e1, e5 = self.info["e1"], self.info["e5"]
        sha12 = e1["pdf"]["sha256"][:12]
        urls = [
            f"/files/pdf/cn-nope-000000000000/{sha12}/a.pdf",                 # unknown edition
            f"/files/pdf/{e1['edition_id']}/000000000000/a.pdf",              # wrong sha
            f"/files/pdf/{e1['edition_id']}/{sha12.upper()}/a.pdf",           # not canonical
            f"/files/pdf/{e1['edition_id']}/{e1['pdf']['sha256']}/a.pdf",     # full sha is not the URL form
            f"/files/pdf/{e5['edition_id']}/{e5['pdf']['sha256'][:12]}/a.pdf",  # catalogued but not stored here
            f"/files/pdf/{e1['edition_id']}/{sha12}",                         # missing filename segment
            f"/files/pdf/{e1['edition_id']}/{sha12}/a/b.pdf",                 # extra segment
        ]
        for url in urls:
            self.assertEqual(self.get(url).status_code, 404, url)

    def test_path_traversal_never_reaches_the_filesystem(self):
        secret = self.tmp / "secret.pdf"
        secret.write_bytes(b"%PDF-secret")
        e1 = self.info["e1"]
        sha12 = e1["pdf"]["sha256"][:12]
        attempts = [
            "/files/pdf/../../secret.pdf",
            "/files/pdf/..%2F..%2Fsecret/000000000000/a.pdf",
            f"/files/pdf/{e1['edition_id']}/{sha12}/..%2F..%2Fsecret.pdf",
            f"/files/pdf/{e1['edition_id']}/..%2F{sha12}/a.pdf",
            f"/files/pdf/%2e%2e/{sha12}/a.pdf",
            f"/files/pdf/{e1['edition_id']}%00/{sha12}/a.pdf",
            "/files/pdf/secret/secret.pdf/..",
        ]
        for url in attempts:
            response = self.get(url)
            self.assertNotIn(b"secret", response.content, url)
            self.assertIn(response.status_code, (400, 404), url)
        # the cosmetic filename may look like a traversal: the catalogued file is still what is served
        response = self.get(f"/files/pdf/{e1['edition_id']}/{sha12}/..%2F..%2Fsecret.pdf")
        if response.status_code == 200:
            self.assertEqual(response.content, self.pdf_bytes("e1"))

    def test_missing_file(self):
        e2 = self.info["e2"]
        path = self.pdf_dir / f"{e2['pdf']['sha256']}.pdf"
        backup = path.read_bytes()
        path.unlink()
        try:
            response = self.get(self.pdf_url("e2"))
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["detail"], "file missing")
        finally:
            path.write_bytes(backup)

    def test_long_cache_header_only_on_successful_bodies(self):
        size = len(self.pdf_bytes("e1"))
        etag = f'"{self.info["e1"]["pdf"]["sha256"]}"'
        immutable = "public, max-age=31536000, immutable"
        self.assertEqual(self.get(self.pdf_url("e1")).headers["cache-control"], immutable)
        self.assertEqual(self.get(self.pdf_url("e1"), headers={"Range": "bytes=0-9"}).headers["cache-control"], immutable)
        bad_range = self.get(self.pdf_url("e1"), headers={"Range": f"bytes={size}-"})
        self.assertEqual((bad_range.status_code, bad_range.headers["cache-control"]), (416, "no-store"))
        not_modified = self.get(self.pdf_url("e1"), headers={"If-None-Match": etag})
        self.assertEqual(not_modified.status_code, 304)
        self.assertNotIn("immutable", not_modified.headers.get("cache-control", ""))
        e1 = self.info["e1"]
        for url in (f"/files/pdf/{e1['edition_id']}/000000000000/a.pdf", "/files/pdf/cn-nope-000000000000/000000000000/a.pdf"):
            response = self.get(url)
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("immutable", response.headers.get("cache-control", ""))

    def test_if_none_match_list_and_star(self):
        etag = f'"{self.info["e1"]["pdf"]["sha256"]}"'
        for header in (f'"x", {etag}', f"W/{etag}", "*"):
            self.assertEqual(self.get(self.pdf_url("e1"), headers={"If-None-Match": header}).status_code, 304, header)
        self.assertEqual(self.get(self.pdf_url("e1"), headers={"If-None-Match": '"x", "y"'}).status_code, 200)

    def test_non_ascii_digits_are_rejected_not_crashed(self):
        size = len(self.pdf_bytes("e1"))
        for header in ("bytes=\u0663-\u0665", "bytes=\u00b2-"):
            response = self.get(self.pdf_url("e1"), headers=[(b"range", header.encode("utf-8"))])
            self.assertEqual(response.status_code, 416, header)
        for url, params in (("/api/library/editions", {"limit": "\u0663"}), ("/api/library/editions", {"offset": "\u00b2"}),
                            (f"/api/library/editions/{self.eid('e1')}/pages", {"from": "\u0661"}),
                            ("/api/library/resolve", {"book_key": "a", "section": "\u0663"}),
                            ("/api/library/resolve", {"items": "aaaaaaaaaaaa.\u0663"}),
                            ("/api/library/resolve", {"edition_id": self.eid("e1"), "sections": "\u0663"}),
                            ("/api/library/downloads", {"row": "\u0663"}), ("/api/library/downloads", {"row": "../x"})):
            self.assertEqual(self.get(url, params=params).status_code, 400, (url, params))
        body = self.get("/api/library/legacy", params={"kind": "jc", "ref": synthetic.REAL_SUN_KEY, "page": "\u0668"}).json()
        self.assertEqual((body["exact"], body["reason"]), (False, "bad-page"))
        self.assertEqual(size, len(self.get(self.pdf_url("e1")).content))

    def test_descriptor_is_closed_when_the_client_goes_away(self):
        import asyncio

        opened = []
        real_open = library._open_pdf

        def tracking_open(path):
            handle = real_open(path)
            opened.append(handle)
            return handle

        e2 = self.info["e2"]
        path = f"/files/pdf/{e2['edition_id']}/{e2['pdf']['sha256'][:12]}/x.pdf"
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
                 "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
                 "headers": [(b"host", b"t")], "client": ("198.51.100.77", 1234), "server": ("t", 80)}

        async def run():
            sent = []

            async def receive():
                await asyncio.sleep(3600)

            async def send(message):
                sent.append(message["type"])
                if message["type"] == "http.response.body":
                    raise OSError("client went away")     # the first body chunk cannot be delivered

            try:
                await self.app(scope, receive, send)
            except BaseException:
                pass
            for _ in range(5):
                await asyncio.sleep(0)
            return sent

        library._open_pdf = tracking_open
        old_chunk = library.STREAM_CHUNK
        library.STREAM_CHUNK = 256            # several chunks even for a tiny file
        try:
            sent = asyncio.run(run())
        finally:
            library._open_pdf = real_open
            library.STREAM_CHUNK = old_chunk
        self.assertIn("http.response.start", sent)
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].closed)

    def test_only_get_and_head(self):
        self.assertEqual(self.client.post(self.pdf_url("e1")).status_code, 405)
        self.assertEqual(self.client.delete(self.pdf_url("e1")).status_code, 405)
        self.assertEqual(self.client.post("/api/library/downloads").status_code, 405)


class PdfAccelTests(Base):
    env_extra = {"PDF_SERVE_MODE": "accel"}

    def test_accel_redirect_headers(self):
        response = self.get(self.pdf_url("e1", download=True), headers={"Range": "bytes=0-9"})
        sha = self.info["e1"]["pdf"]["sha256"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["x-accel-redirect"], f"/_protected_pdf/{sha}.pdf")
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertTrue(response.headers["content-disposition"].startswith("attachment; filename*=UTF-8''"))
        self.assertEqual(response.headers["cache-control"], "public, max-age=31536000, immutable")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertNotIn("content-range", response.headers)
        self.assertEqual(self.client.head(self.pdf_url("e1")).headers["x-accel-redirect"], f"/_protected_pdf/{sha}.pdf")

    def test_allow_list_applies_before_redirect(self):
        e1 = self.info["e1"]
        for url in (f"/files/pdf/cn-nope-000000000000/{e1['pdf']['sha256'][:12]}/a.pdf",
                    f"/files/pdf/{e1['edition_id']}/000000000000/a.pdf",
                    self.pdf_url("e5")):
            response = self.get(url)
            self.assertEqual(response.status_code, 404, url)
            self.assertNotIn("x-accel-redirect", response.headers)

    def test_unmounted_pdf_root_trusts_the_catalog(self):
        app, _ = self.make_app({"PDF_SERVE_MODE": "accel", "LIBRARY_PDF_ROOT": str(self.tmp / "not-mounted"),
                                "LIBRARY_ACCEL_PREFIX": "/_x"})
        response = TestClient(app).get(self.pdf_url("e1"))
        self.assertEqual(response.headers["x-accel-redirect"], f"/_x/{self.info['e1']['pdf']['sha256']}.pdf")

    def test_default_mode_is_accel_and_bad_prefix_is_ignored(self):
        settings = library.Settings.from_env(self.data_root, {"LIBRARY_ACCEL_PREFIX": "/a b\r\nX: 1"})
        self.assertEqual((settings.serve_mode, settings.accel_prefix), ("accel", "/_protected_pdf/"))
        self.assertEqual(str(settings.pdf_root), "/srv/sun-textbooks/pdf")


# ── Rate limiting ────────────────────────────────────────────────────────────

class RateLimitTests(Base):
    env_extra = {"LIBRARY_RATE_JSON_BURST": "3", "LIBRARY_RATE_JSON_PER_SECOND": "0.5",
                 "LIBRARY_RATE_PDF_BURST": "2", "LIBRARY_RATE_PDF_PER_SECOND": "1"}

    def test_json_bucket(self):
        client = TestClient(self.app, client=("203.0.113.9", 4000))
        self.assertEqual([client.get("/api/library/release").status_code for _ in range(3)], [200, 200, 200])
        response = client.get("/api/library/editions")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "2")
        self.assertEqual(TestClient(self.app, client=("203.0.113.10", 4000)).get("/api/library/release").status_code, 200)
        self.clock.advance(2)
        self.assertEqual(client.get("/api/library/editions").status_code, 200)

    def test_pdf_bucket_is_separate(self):
        client = TestClient(self.app, client=("203.0.113.20", 4000))
        self.assertEqual([client.get(self.pdf_url("e1"), headers={"Range": "bytes=0-1"}).status_code for _ in range(3)], [206, 206, 429])
        self.assertEqual(client.get("/api/library/release").status_code, 200)

    def test_x_real_ip_only_from_loopback_peer(self):
        remote = TestClient(self.app, client=("198.51.100.7", 4000))
        for index in range(3):
            self.assertEqual(remote.get("/api/library/release", headers={"X-Real-IP": f"10.0.0.{index}"}).status_code, 200)
        # spoofed header did not create fresh buckets
        self.assertEqual(remote.get("/api/library/release", headers={"X-Real-IP": "10.0.0.99"}).status_code, 429)
        proxy = TestClient(self.app, client=("127.0.0.1", 4000))
        for index in range(5):
            self.assertEqual(proxy.get("/api/library/release", headers={"X-Real-IP": f"192.0.2.{index}"}).status_code, 200)

    def test_client_key(self):
        def req(peer, real=None):
            return SimpleNamespace(client=SimpleNamespace(host=peer), headers=({"x-real-ip": real} if real else {}))
        key = library.client_key
        self.assertEqual(key(req("127.0.0.1", "2001:db8:1:2:aaaa::1")), "ip6:2001:db8:1:2::/64")
        self.assertEqual(key(req("::1", "2001:db8:1:2:bbbb::2")), "ip6:2001:db8:1:2::/64")
        self.assertNotEqual(key(req("::1", "2001:db8:1:3::2")), key(req("::1", "2001:db8:1:2::2")))
        self.assertEqual(key(req("127.0.0.1", "::ffff:192.0.2.1")), "ip4:192.0.2.1")
        self.assertEqual(key(req("127.0.0.1", "not-an-ip")), "ip4:127.0.0.1")
        self.assertEqual(key(req("172.17.0.1", "192.0.2.1")), "ip4:172.17.0.1")
        nets = library.parse_networks
        self.assertEqual(key(req("172.17.0.1", "192.0.2.1"), nets("172.17.0.1")), "ip4:192.0.2.1")
        self.assertEqual(key(req("172.17.0.9", "192.0.2.1"), nets("172.17.0.0/16")), "ip4:192.0.2.1")
        self.assertEqual(key(req("172.18.0.1", "192.0.2.1"), nets("172.17.0.0/16")), "ip4:172.18.0.1")
        self.assertEqual(key(req("::ffff:172.17.0.1", "192.0.2.1"), nets("172.17.0.1/32")), "ip4:192.0.2.1")
        self.assertEqual(key(req("fd00::2", "192.0.2.1"), nets("fd00::/64")), "ip4:192.0.2.1")
        # a typo must never trust the world, and junk is ignored
        self.assertEqual(nets("0.0.0.0/0, ::/0, 10.0.0.0/7, nonsense, 300.1.1.1, , 10.0.0.0/8"), nets("10.0.0.0/8"))
        self.assertEqual(key(req("203.0.113.5", "192.0.2.1"), nets("0.0.0.0/0")), "ip4:203.0.113.5")
        self.assertEqual(key(req("testclient")), "peer:testclient")
        self.assertEqual(key(SimpleNamespace(client=None, headers={})), "peer:unknown")

    def test_bucket_table_is_bounded(self):
        clock = FakeClock()
        limiter = library.RateLimiter(2, 1.0, clock, max_keys=50)
        for index in range(500):
            limiter.take(f"k{index}")
            clock.advance(0.1)
        self.assertLessEqual(len(limiter._buckets), 50)

    def test_fresh_keys_cannot_refill_an_exhausted_bucket_in_use(self):
        clock = FakeClock()
        limiter = library.RateLimiter(2, 0.001, clock, max_keys=100)
        self.assertEqual([limiter.take("victim")[0] for _ in range(3)], [True, True, False])
        for index in range(5000):          # flood of new keys while the victim keeps trying
            limiter.take(f"flood-{index}")
            if index % 50 == 0:
                self.assertFalse(limiter.take("victim")[0], index)
        self.assertLessEqual(len(limiter._buckets), 100)
        self.assertFalse(limiter.take("victim")[0])


# ── Release written in the shape of scripts/catalog/build_catalog.py ─────────

class GeneratorShapeTests(Base):
    """`contract` instead of `schema`, download rows under `rows`, editions that are
    catalogued but not acquired (`pdf: null`), `storage.vps: null` before files are
    placed, `held_other_edition_ids`, extra keys everywhere."""

    RID = "20261010T101500Z-0a1b2c3d"
    SID_A = "aaaaaaaa-1111-4222-8333-444444444444"
    SID_B = "bbbbbbbb-1111-4222-8333-444444444444"
    SID_C = "cccccccc-1111-4222-8333-444444444444"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        sha_a, sha_b = cls.info["e1"]["pdf"]["sha256"], cls.info["e3"]["pdf"]["sha256"]
        cls.id_a, cls.id_b, cls.id_c = f"cn-{cls.SID_A}-{sha_a[:12]}", f"cn-{cls.SID_B}-{sha_b[:12]}", f"cn-{cls.SID_C}-pending"

        def edition(eid, sid, pdf, status, **legacy):
            return {
                "edition_id": eid, "work_id": f"cn/高中/语文/{sid[:4]}", "region": "CN", "stage": "高中", "subject": "语文",
                "title": "普通高中教科书·语文 必修 上册", "volume": "必修 上册", "publisher": "人民教育出版社", "edition_label": "统编版",
                "source": {"provider": "smartedu", "source_id": sid, "catalog_module": "x", "online_time": None,
                           "update_time": None, "ti_md5": None, "ti_size": None, "retrieved_at": "2026-10-10T10:15:00Z",
                           "retrieved_via": "jks-ai", "source_id_basis": "identifier:jc-content-id", "platform_current": True},
                "pdf": pdf, "storage": {"vps": None, "drive": None, "upstream": {"available": True, "url": f"https://jks-ai.bdfz.net/pdf/{sid}"}},
                "legacy": {"sun_book_key": None, "jc_short_key": None, "jks_slug": None, "basis": None, "in_sun_search": False,
                           "alias_candidate": None, "mapped_pages": 0, "offset": None, **legacy},
                "corpus": {"relation": "none", "basis": "no PDF held", "coverage": None, "verification": "not-possible"},
                "processing": {"ocr_version": None, "index_version": None, "page_map_version": "page-map-v1", "extractor_version": "x"},
                "printed": None, "status": status,
            }

        def pdf(name):
            return {**cls.info[name]["pdf"], "producer": "x"}

        a = edition(cls.id_a, cls.SID_A, pdf("e1"), "active", sun_book_key=synthetic.REAL_SUN_KEY,
                    jc_short_key=library.short_key_for(synthetic.REAL_SUN_KEY), basis="bytes:source-pdf")
        a["corpus"] = {"relation": "same-bytes", "basis": "bytes", "coverage": 1.0, "verification": "bytes"}
        b = edition(cls.id_b, cls.SID_B, pdf("e3"), "superseded")
        b["source"]["platform_current"] = False
        c = edition(cls.id_c, cls.SID_C, None, "pending-verification")
        rows_a = [{"pdf_page": n, "printed_label": None, "printed_verified": False, "legacy_image_index": n - 1,
                   "legacy_basis": "same-bytes", "ocr_page": None, "text_layer": "text"} for n in range(1, 13)]
        page_map = {"contract": "sun-catalog-v1/page-map", "page_map_version": "page-map-v1", "edition_id": cls.id_a,
                    "pdf_sha256": sha_a, "pdf_pages": 12, "printed": {"verified": False, "evidence": {}},
                    "legacy": {"mapped_pages": 12, "coverage": 1.0, "evidence": {}}, "ocr": {}, "pages": rows_a}

        def row(sid, edition_id, **over):
            return {"source_id": sid, "stage": "高中", "subject": "语文", "title": "普通高中教科书·语文 必修 上册",
                    "edition_label": "统编版", "publisher": "人民教育出版社", "cover": None, "size_bytes": 1234,
                    "sources": [{"kind": "upstream", "url": f"https://jks-ai.bdfz.net/pdf/{sid}"}],
                    "edition_id": edition_id, "held_other_edition_ids": [], **over}

        files = {
            "catalog.json": {"contract": "sun-catalog-v1", "release_id": cls.RID, "generated_at": "2026-10-10T10:15:00Z",
                             "region_default": "CN", "editions": [a, b, c], "reader_books": []},
            "downloads.json": {"contract": "sun-catalog-v1/downloads", "release_id": cls.RID, "groups": [], "not_enumerated": [],
                               "rows": [row(cls.SID_A, cls.id_a), row(cls.SID_B, None, held_other_edition_ids=[cls.id_b], title=None),
                                        row(cls.SID_C, None)]},
            "RELEASE.json": {"contract": "sun-catalog-v1", "release_id": cls.RID, "generated_at": "2026-10-10T10:15:00Z",
                             "tool": {}, "inputs": {}, "counts": {}},
            f"page-maps/{cls.id_a}.json": page_map,
        }
        cls.gen_root = cls.tmp / "gen" / "catalog"
        release_dir = cls.gen_root / "releases" / cls.RID
        (release_dir / "page-maps").mkdir(parents=True)
        lines = []
        for rel, payload in sorted(files.items()):
            data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")
            (release_dir / rel).write_bytes(data)
            lines.append(f"{hashlib.sha256(data).hexdigest()}  {rel}")
        (release_dir / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
        synthetic.set_current(cls.gen_root, cls.RID)

    def client_for(self, **extra):
        app, lib = self.make_app({"LIBRARY_CATALOG_ROOT": str(self.gen_root), **extra})
        return TestClient(app), lib

    def test_loads_and_nothing_is_previewable_until_storage_is_recorded(self):
        client, lib = self.client_for()
        body = client.get("/api/library/release").json()
        self.assertTrue(body["available"], lib.state.error)
        self.assertEqual(body["release_id"], self.RID)
        self.assertEqual((body["counts"]["editions"], body["counts"]["previewable"], body["counts"]["downloads"]), (3, 0, 3))
        self.assertEqual(client.get("/api/library/editions").json()["total"], 0)
        self.assertEqual(client.get(f"/files/pdf/{self.id_a}/{self.id_a[-12:]}/a.pdf").status_code, 404)
        self.assertEqual(client.get("/api/library/resolve", params={"book_key": synthetic.REAL_SUN_KEY, "section": "3"}).json()["reason"], "no-preview")
        rows = client.get("/api/library/downloads").json()["downloads"]
        self.assertEqual([[s["kind"] for s in r["sources"]] for r in rows], [["upstream"], ["upstream"], ["upstream"]])

    def test_pending_edition_without_pdf(self):
        client, _ = self.client_for()
        body = client.get(f"/api/library/editions/{self.id_c}").json()["edition"]
        self.assertEqual((body["previewable"], body["file"], body["pdf"], body["pages"], body["status"]),
                         (False, None, None, 0, "pending-verification"))
        self.assertEqual(client.get(f"/api/library/editions/{self.id_c}/pages").status_code, 404)
        self.assertEqual(client.get(f"/files/pdf/{self.id_c}/000000000000/a.pdf").status_code, 404)

    def test_present_policy_uses_files_found_at_load(self):
        client, _ = self.client_for(LIBRARY_PREVIEW_POLICY="present")
        body = client.get("/api/library/release").json()
        self.assertEqual((body["counts"]["previewable"], body["counts"]["active_previewable"], body["counts"]["downloads"]), (2, 1, 4))
        self.assertEqual([e["edition_id"] for e in client.get("/api/library/editions").json()["editions"]], [self.id_a])
        self.assertEqual(client.get(f"/files/pdf/{self.id_a}/{self.id_a[-12:]}/a.pdf", headers={"Range": "bytes=0-3"}).content, b"%PDF")
        resolved = client.get("/api/library/resolve", params={"book_key": synthetic.REAL_SUN_KEY, "section": "3"}).json()
        self.assertEqual((resolved["available"], resolved["edition_id"], resolved["pdf_page"]), (True, self.id_a, 4))
        rows = {r["row_id"]: r for r in client.get("/api/library/downloads").json()["downloads"]}
        self.assertEqual(set(rows), {self.id_a, self.id_b, self.SID_B, self.SID_C})
        self.assertEqual([s["kind"] for s in rows[self.id_a]["sources"]], ["vps", "upstream"])
        # the superseded file we hold is its own row (本站 only); the platform row keeps upstream only
        self.assertEqual([s["kind"] for s in rows[self.id_b]["sources"]], ["vps"])
        self.assertEqual([s["kind"] for s in rows[self.SID_B]["sources"]], ["upstream"])
        self.assertIsNone(rows[self.SID_B]["edition_id"])
        self.assertEqual(rows[self.SID_B]["title"], "未命名教材")
        self.assertEqual(rows[self.SID_B]["other_editions"], [{"edition_id": self.id_b, "edition_label": "统编版",
                                                                "status": "superseded", "previewable": True,
                                                                "platform_current": False,
                                                                "retrieved_at": "2026-10-10T10:15:00Z"}])
        self.assertIs(rows[self.id_b]["platform_current"], False)
        self.assertIs(rows[self.id_a]["platform_current"], True)

    def test_foreign_contract_is_rejected(self):
        root = self.tmp / "gen2" / "catalog"
        shutil.copytree(self.gen_root, root)
        target = root / "releases" / self.RID / "RELEASE.json"
        data = target.read_text().replace('"contract": "sun-catalog-v1"', '"contract": "sun-catalog-v2"').encode()
        target.write_bytes(data)
        sums = root / "releases" / self.RID / "SHA256SUMS"
        sums.write_text("\n".join(line for line in sums.read_text().splitlines() if not line.endswith("RELEASE.json"))
                        + f"\n{hashlib.sha256(data).hexdigest()}  RELEASE.json\n")
        _, lib = self.client_for(LIBRARY_CATALOG_ROOT=str(root))
        self.assertIsNone(lib.state.get())
        self.assertEqual(lib.state.error, "unsupported catalog schema")


# ── Release revision 2: platform_current / newer edition / text identity ─────

class RevisionTwoTests(Base):
    """New optional fields.  The same loader must also keep loading the old shape
    (GeneratorShapeTests and the synthetic release above carry none of them)."""

    SID = "dddddddd-1111-4222-8333-444444444444"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        old_pdf, cur_pdf = cls.info["e3"]["pdf"], cls.info["e1"]["pdf"]
        cls.old_id = f"cn-{cls.SID}-{old_pdf['sha256'][:12]}"
        cls.new_id = f"cn-{cls.SID}-pending"
        cls.cur_id = f"cn-eeeeeeee-1111-4222-8333-444444444444-{cur_pdf['sha256'][:12]}"

        def edition(eid, sid, pdf, status, **over):
            record = {
                "edition_id": eid, "work_id": "cn/初中/物理/8s", "region": "CN", "stage": "初中", "subject": "物理",
                "title": "义务教育教科书·物理 八年级 上册", "volume": "八年级上册", "publisher": "人民教育出版社",
                "edition_label": "人教版", "metadata_basis": "smartedu-details",
                "source": {"provider": "smartedu", "source_id": sid, "id_basis": "bytes:sha256=learn-books",
                           "retrieved_at": "2026-03-15T08:09:07Z", "update_time": None, "online_time": None},
                "pdf": pdf, "storage": {"vps": {"path": f"pdf/{pdf['sha256']}.pdf"} if pdf else None, "drive": None,
                                        "upstream": {"available": True}},
                "legacy": {"sun_book_key": None, "jc_short_key": None, "jks_slug": None},
                "corpus": {"relation": "none", "basis": "x", "coverage": None, "text_identity": None},
                "processing": {}, "status": status,
            }
            record.update(over)
            return record

        old = edition(cls.old_id, cls.SID, dict(old_pdf), "active", platform_current=False, newer_edition_id=cls.new_id,
                      corpus={"relation": "same-bytes", "basis": "bytes", "coverage": 1.0, "text_identity": "same-file"},
                      legacy={"sun_book_key": synthetic.SECOND_SUN_KEY, "jc_short_key": None, "jks_slug": None})
        new = edition(cls.new_id, cls.SID, None, "pending-verification", platform_current=True,
                      supersedes_edition_id=cls.old_id)
        new["source"]["update_time"] = "2026-08-18T00:00:00Z"
        cur = edition(cls.cur_id, "eeeeeeee-1111-4222-8333-444444444444", dict(cur_pdf), "active", platform_current=True,
                      stage="高中", subject="语文", title="普通高中教科书·语文 必修 上册", work_id="cn/高中/语文/bx-s",
                      newer_edition_id="cn-does-not-exist-000000000000",
                      corpus={"relation": "aligned", "basis": "anchors", "coverage": 0.9,
                              "text_identity": "page-aligned-other-printing"},
                      legacy={"sun_book_key": synthetic.REAL_SUN_KEY, "jc_short_key": None, "jks_slug": None})
        maps = {
            cls.old_id: {"edition_id": cls.old_id, "pdf_sha256": old_pdf["sha256"], "pages": [
                {"pdf_page": n, "printed_label": None, "printed_verified": False, "legacy_image_index": n - 1, "ocr_page": None}
                for n in range(1, old_pdf["pages"] + 1)]},
            cls.cur_id: {"edition_id": cls.cur_id, "pdf_sha256": cur_pdf["sha256"], "pages": [
                {"pdf_page": n, "printed_label": None, "printed_verified": False, "legacy_image_index": n + 1, "ocr_page": None}
                for n in range(1, cur_pdf["pages"] + 1)]},
        }
        rows = [
            {"source_id": cls.SID, "stage": "初中", "subject": "物理", "title": "义务教育教科书·物理 八年级 上册",
             "edition_label": "人教版", "publisher": "人民教育出版社", "cover": f"https://jks-ai.bdfz.net/cover/{cls.SID}",
             "size_bytes": None, "sources": [{"kind": "upstream", "url": f"https://jks-ai.bdfz.net/pdf/{cls.SID}"}],
             "edition_id": None, "held_other_edition_ids": [cls.old_id]},
            {"source_id": "eeeeeeee-1111-4222-8333-444444444444", "stage": "高中", "subject": "语文",
             "title": "普通高中教科书·语文 必修 上册", "edition_label": "统编版", "publisher": "人民教育出版社", "cover": None,
             "size_bytes": None, "sources": [{"kind": "upstream", "url": "https://jks-ai.bdfz.net/pdf/eeeeeeee-1111-4222-8333-444444444444"}],
             "edition_id": cls.cur_id, "held_other_edition_ids": []},
        ]
        cls.rev_root = cls.tmp / "rev2" / "catalog"
        synthetic.write_release(cls.rev_root / "releases" / "r-rev2", "r-rev2", [old, new, cur], rows, maps)
        synthetic.set_current(cls.rev_root, "r-rev2")
        app, cls.rev_lib = cls.make_app({"LIBRARY_CATALOG_ROOT": str(cls.rev_root)})
        cls.rev = TestClient(app)

    def test_release_loads(self):
        body = self.rev.get("/api/library/release").json()
        self.assertTrue(body["available"], self.rev_lib.state.error)
        self.assertEqual((body["counts"]["editions"], body["counts"]["previewable"], body["counts"]["downloads"]), (3, 2, 3))

    def test_edition_exposes_the_new_fields(self):
        body = self.rev.get(f"/api/library/editions/{self.old_id}").json()["edition"]
        self.assertIs(body["platform_current"], False)
        self.assertEqual(body["newer_edition_id"], self.new_id)
        self.assertEqual(body["source"]["id_basis"], "bytes:sha256=learn-books")
        self.assertEqual(body["metadata_basis"], "smartedu-details")
        self.assertEqual(body["corpus"]["text_identity"], "same-file")
        self.assertTrue(body["corpus"]["text_identity_declared"])
        self.assertEqual(body["newer_edition"], {
            "edition_id": self.new_id, "title": "义务教育教科书·物理 八年级 上册", "edition_label": "人教版",
            "publisher": "人民教育出版社", "status": "pending-verification", "pages": None, "previewable": False,
            "platform_current": True, "update_time": "2026-08-18T00:00:00Z", "download_row_id": self.SID})
        # the page of a not-current edition offers this site's file only: no upstream link under its name
        self.assertTrue(body["file"]["download_url"].startswith(f"/files/pdf/{self.old_id}/"))
        self.assertEqual(body["sources"], [])
        other = self.rev.get(f"/api/library/editions/{self.new_id}").json()["edition"]
        self.assertEqual(other["supersedes_edition"]["edition_id"], self.old_id)
        self.assertIs(other["platform_current"], True)
        self.assertIsNone(other["file"])
        card = self.rev.get("/api/library/editions", params={"phase": "初中"}).json()["editions"][0]
        self.assertEqual((card["platform_current"], card["newer_edition_id"]), (False, self.new_id))

    def test_dangling_reference_is_dropped_not_fatal(self):
        body = self.rev.get(f"/api/library/editions/{self.cur_id}").json()["edition"]
        self.assertIsNone(body["newer_edition_id"])
        self.assertIsNone(body["newer_edition"])

    def test_text_identity_travels_with_resolved_links(self):
        body = self.rev.get("/api/library/resolve", params={"book_key": synthetic.REAL_SUN_KEY, "section": "3"}).json()
        self.assertEqual((body["available"], body["pdf_page"], body["text_identity"]), (True, 2, "page-aligned-other-printing"))
        body = self.rev.get("/api/library/resolve", params={"book_key": synthetic.SECOND_SUN_KEY, "section": "3"}).json()
        self.assertEqual((body["pdf_page"], body["text_identity"]), (4, "same-file"))
        detail = self.rev.get(f"/api/library/editions/{self.cur_id}").json()["edition"]
        self.assertEqual(detail["badges"]["text_identity"], "page-aligned-other-printing")

    def test_two_files_of_one_book_are_two_labelled_rows(self):
        rows = {r["row_id"]: r for r in self.rev.get("/api/library/downloads", params={"stage": "初中"}).json()["downloads"]}
        self.assertEqual(set(rows), {self.SID, self.old_id})
        held, platform = rows[self.old_id], rows[self.SID]
        self.assertEqual([s["kind"] for s in held["sources"]], ["vps"])            # 本站 only, this edition only
        self.assertIs(held["platform_current"], False)
        self.assertEqual(held["retrieved_at"], "2026-03-15T08:09:07Z")
        self.assertEqual(held["newer_edition"]["download_row_id"], self.SID)
        self.assertEqual([s["kind"] for s in platform["sources"]], ["upstream"])   # never a 本站 source here
        self.assertIsNone(platform["edition_id"])
        self.assertEqual([(o["edition_id"], o["platform_current"]) for o in platform["other_editions"]], [(self.old_id, False)])
        self.assertEqual(platform["cover"], f"https://jks-ai.bdfz.net/cover/{self.SID}")

    def test_row_filter_addresses_one_entry(self):
        body = self.rev.get("/api/library/downloads", params={"row": self.SID}).json()
        self.assertEqual([r["row_id"] for r in body["downloads"]], [self.SID])
        self.assertEqual(self.rev.get("/api/library/downloads", params={"row": "nope"}).json()["total"], 0)

    def test_old_shape_gets_the_cautious_text_identity(self):
        # no corpus.text_identity in the synthetic release: aligned/partial are treated as another printing
        e1 = self.get(f"/api/library/editions/{self.eid('e1')}").json()["edition"]
        self.assertEqual((e1["corpus"]["text_identity"], e1["corpus"]["text_identity_declared"]), ("page-aligned-other-printing", False))
        self.assertIsNone(e1["platform_current"])
        self.assertIsNone(e1["newer_edition"])
        e2 = self.get(f"/api/library/editions/{self.eid('e2')}").json()["edition"]
        self.assertIsNone(e2["corpus"]["text_identity"])

    def test_no_edition_is_called_newest(self):
        texts = [(ROOT / "frontend" / "assets" / name).read_text(encoding="utf-8") for name in ("library.js", "library.css")]
        texts.append((ROOT / "backend" / "library.py").read_text(encoding="utf-8"))
        for text in texts:
            self.assertNotIn("最新", text)
            self.assertNotRegex(text, r"(?i)\blatest\b")


# ── Helpers ──────────────────────────────────────────────────────────────────

class HelperTests(unittest.TestCase):
    def test_derive_filename(self):
        self.assertEqual(library.derive_filename("语文 必修", "2019 版", "cn-x"), "语文 必修（2019 版）.pdf")
        self.assertEqual(library.derive_filename("a/b\\c:d*e?f\"g<h>i|j\r\nk", None, "cn-x"), "a b c d e f g h i j k.pdf")
        self.assertEqual(library.derive_filename("../..", None, "cn-x"), "cn-x.pdf")
        self.assertEqual(library.derive_filename("", None, "cn-x"), "cn-x.pdf")
        self.assertLessEqual(len(library.derive_filename("长" * 300, None, "cn-x")), 84)

    def test_safe_https_url(self):
        hosts = ("jks-ai.bdfz.net", "smartedu.cn")
        ok = library.safe_https_url
        self.assertEqual(ok("https://jks-ai.bdfz.net/pdf/1", hosts), "https://jks-ai.bdfz.net/pdf/1")
        self.assertEqual(ok("https://basic.smartedu.cn/x", hosts), "https://basic.smartedu.cn/x")
        for bad in ("http://jks-ai.bdfz.net/pdf/1", "https://evilsmartedu.cn/x", "https://smartedu.cn.evil.example/x",
                    "https://user:pw@jks-ai.bdfz.net/", "https://jks-ai.bdfz.net:8443/", "javascript:alert(1)",
                    "https://jks-ai.bdfz.net/a b", "https://jks-ai.bdfz.net/\"><script>", "//jks-ai.bdfz.net/x", None, 5,
                    "https://jks-ai.bdfz.net/" + "a" * 2000):
            self.assertIsNone(ok(bad, hosts), bad)

    def test_short_key_rule(self):
        self.assertEqual(library.short_key_for("高中_语文_普通高中教科书_语文必修_上册"), "1082bfb4a71a")


if __name__ == "__main__":
    unittest.main()
