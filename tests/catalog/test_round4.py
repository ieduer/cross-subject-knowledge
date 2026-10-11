"""Offline tests: reassembled editions, unmapped-page hints, covers, lesson-page proposals."""
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "catalog"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import covers  # noqa: E402
import page_map  # noqa: E402
import reassemble  # noqa: E402
import validate_release  # noqa: E402
import yw_candidates  # noqa: E402
from common import dumps, jc_short_key, write_json  # noqa: E402
from test_build_validate import (HOLD, KEY_B, SHA_B, SID_B, Fixture, edition, page_recs, resum)  # noqa: E402

try:
    import pymupdf
except ImportError:  # pragma: no cover
    pymupdf = None


class UnmappedHintTests(unittest.TestCase):
    def test_every_index_is_mapped_or_listed_with_its_nearest_mapped_neighbour(self):
        mapped = {0: 1, 1: 2, 5: 7, 6: 8}
        um = page_map.legacy_unmapped(mapped, 9)
        self.assertEqual([u["legacy_image_index"] for u in um], [2, 3, 4, 7, 8])
        near = {u["legacy_image_index"]: u["nearest_mapped"] for u in um}
        self.assertEqual(near[2], {"legacy_image_index": 1, "pdf_page": 2, "distance": 1})
        self.assertEqual(near[3], {"legacy_image_index": 1, "pdf_page": 2, "distance": 2})     # tie -> the earlier one
        self.assertEqual(near[4], {"legacy_image_index": 5, "pdf_page": 7, "distance": 1})
        self.assertEqual(near[8], {"legacy_image_index": 6, "pdf_page": 8, "distance": 2})
        self.assertTrue(all("pdf_page" not in u for u in um))                                   # a hint, not a mapping

    def test_edge_cases(self):
        self.assertEqual(page_map.legacy_unmapped({0: 1, 1: 2}, 2), [])
        self.assertIsNone(page_map.legacy_unmapped({0: 1}, None))
        self.assertEqual(page_map.legacy_unmapped({}, 2), [{"legacy_image_index": 0, "nearest_mapped": None},
                                                          {"legacy_image_index": 1, "nearest_mapped": None}])

    def test_page_map_carries_the_list_and_counts_nothing_as_mapped(self):
        recs = page_recs(40, 2)
        img = {k: {"pdf_page": k + 1, "score": 0.95, "second": 0.3} for k in range(0, 16)}
        pm = page_map.build_page_map("cn-x-0000000000000000", "0" * 64, {"extractor_version": "t", "page_texts": recs},
                                     {"mode": "content", "legacy_pages": 40, "image_rows": img, "informative": 40})
        self.assertEqual(pm["legacy"]["mapped_pages"], 16)
        self.assertEqual(len(pm["legacy_unmapped"]), 24)
        self.assertEqual(pm["legacy_unmapped"][-1]["nearest_mapped"], {"legacy_image_index": 15, "pdf_page": 16, "distance": 24})
        self.assertTrue(all(r["legacy_image_index"] is None for r in pm["pages"][16:]))

    def test_reassembled_mode_is_exact_and_needs_equal_page_counts(self):
        recs = [{"pdf_page": p, "chars": 0, "kind": "ocr_needed", "text": "", "margin": {"top": [], "bottom": []}}
                for p in range(1, 13)]
        pm = page_map.build_page_map("cn-x-1", "1" * 64, {"extractor_version": "t", "page_texts": recs},
                                     {"mode": "reassembled", "legacy_pages": 12})
        self.assertEqual([(r["legacy_image_index"], r["legacy_basis"]) for r in pm["pages"]],
                         [(i, "reassembled") for i in range(12)])
        self.assertEqual((pm["legacy"]["evidence"]["relation"], pm["legacy_unmapped"]), ("legacy-image-source", []))
        self.assertTrue(all(r["printed_label"] is None and not r["printed_verified"] for r in pm["pages"]))
        with self.assertRaises(ValueError):
            page_map.build_page_map("cn-x-1", "1" * 64, {"extractor_version": "t", "page_texts": recs},
                                    {"mode": "reassembled", "legacy_pages": 13})


@unittest.skipIf(pymupdf is None, "pymupdf not installed")
class ReassembleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="reassemble-test-"))
        self.root = self.tmp / "pages"
        (self.root / "k1").mkdir(parents=True)
        self.manifest = {}
        for i in range(5):
            data = f"fake webp {i}".encode() * 10
            (self.root / "k1" / f"p{i}.webp").write_bytes(data)
            self.manifest[f"k1/p{i}.webp"] = hashlib.sha256(data).hexdigest()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def decode(path, tmp):
        i = int(path.stem[1:])
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 300), False)
        pix.clear_with(40 * i + 20)
        return pix

    def test_one_page_per_image_in_order_with_honest_metadata(self):
        out = self.tmp / "a.pdf"
        info = reassemble.reassemble("k1", 5, "某书（旧版，由旧页图重组）", self.root, self.manifest, out, decode=self.decode)
        with pymupdf.open(str(out)) as doc:
            self.assertEqual(doc.page_count, 5)
            self.assertEqual((round(doc[0].rect.width), round(doc[0].rect.height)), (144, 216))   # 200x300 px at 100 dpi
            self.assertTrue(all(page.get_text().strip() == "" for page in doc))                     # no text layer
            self.assertTrue(all(len(page.get_images()) == 1 for page in doc))
            self.assertIn("reassembled from legacy page images", doc.metadata["producer"])
            self.assertEqual(doc.metadata["title"], "某书（旧版，由旧页图重组）")
        rows = info["page_sources"]
        self.assertEqual([(r["pdf_page"], r["legacy_image_index"]) for r in rows], [(i + 1, i) for i in range(5)])
        self.assertEqual([r["source_sha256"] for r in rows], [self.manifest[f"k1/p{i}.webp"] for i in range(5)])
        self.assertEqual((info["encoding"]["embed"], info["encoding"]["text_layer"]), ("JPEG (DCTDecode)", False))

    def test_same_images_give_the_same_file(self):
        a, b = self.tmp / "a.pdf", self.tmp / "b.pdf"
        reassemble.reassemble("k1", 5, "t", self.root, self.manifest, a, decode=self.decode)
        reassemble.reassemble("k1", 5, "t", self.root, self.manifest, b, decode=self.decode)
        self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_an_image_that_is_not_the_published_one_stops_the_build(self):
        (self.root / "k1" / "p3.webp").write_bytes(b"tampered")
        with self.assertRaises(RuntimeError):
            reassemble.reassemble("k1", 5, "t", self.root, self.manifest, self.tmp / "a.pdf", decode=self.decode)


@unittest.skipIf(pymupdf is None, "pymupdf not installed")
class CoverTests(unittest.TestCase):
    def test_cover_is_page_one_at_the_requested_width(self):
        tmp = Path(tempfile.mkdtemp(prefix="cover-test-"))
        try:
            doc = pymupdf.open()
            doc.new_page(width=420, height=595).insert_text((50, 100), "COVER", fontsize=40)
            doc.new_page(width=420, height=595).insert_text((50, 100), "second page", fontsize=40)
            path = tmp / "a.pdf"
            doc.save(str(path))
            doc.close()
            seen = {}

            def keep_png(png):
                pix = pymupdf.Pixmap(png)
                seen["size"] = (pix.width, pix.height)
                return png
            a = covers.render_cover(path, 240, encode=keep_png)
            self.assertEqual(seen["size"], (240, 340))
            self.assertEqual(a, covers.render_cover(path, 240, encode=keep_png))                    # deterministic
            covers.render_cover(path, 480, encode=keep_png)
            self.assertEqual(seen["size"][0], 480)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class LessonPageProposalTests(unittest.TestCase):
    def test_verdicts(self):
        v = yw_candidates.verdict
        self.assertEqual(v([(0.83, 25), (0.06, 24)], True), "confident")
        self.assertEqual(v([(0.46, 24), (0.45, 23), (0.03, 22)], True), "spans-two-pages")
        self.assertEqual(v([(0.44, 89), (0.18, 95)], True), "probable")
        self.assertEqual(v([(0.23, 38), (0.19, 39)], True), "unclear")
        self.assertEqual(v([(0.46, 24), (0.45, 40)], True), "unclear")                 # not adjacent: not a re-flow
        self.assertEqual(v([(0.05, 3)], True), "no-counterpart")
        self.assertEqual(v([], True), "no-counterpart")
        self.assertEqual(v([(0.9, 3)], False), "no-counterpart")                       # blank legacy page

    def test_candidates_are_proposals_with_evidence(self):
        def text(i):
            return f"第{i}页讲的是编号{i * 7919}的课文，作者是角色{i * 104729}，写于城市{i * 1299709}的一个清晨。" * 4
        pdf = [text(i) for i in range(1, 31)]
        pdf[14] = "新换入的课文，旧版没有这一篇。" * 10
        book = {"shortKey": "k", "title": "某册", "toc": [{"label": "第一单元", "page": 0, "level": 1},
                                                         {"label": "5 某课文/某人", "page": 9, "level": 2}]}
        mapped = {i: i + 1 for i in range(30) if i not in (11, 14)}
        corpus = {11: text(12), 14: "旧版才有的课文，新版已经删去，换成了别的文章。" * 8}
        hit = yw_candidates.candidates(book, 11, corpus, pdf, mapped)
        self.assertEqual((hit["verdict"], hit["candidates"][0]["pdf_page"], hit["legacy_lesson"]), ("confident", 12, "5 某课文/某人"))
        self.assertGreaterEqual(hit["candidates"][0]["similarity"], 0.9)
        self.assertLessEqual(len(hit["legacy_excerpt"]), 120)
        gone = yw_candidates.candidates(book, 14, corpus, pdf, mapped)
        self.assertEqual(gone["verdict"], "no-counterpart")
        self.assertEqual([p["pdf_page"] for p in gone["current_edition_pages_in_that_position"]], [15])
        self.assertIn("新换入的课文", gone["current_edition_pages_in_that_position"][0]["pdf_excerpt"])
        self.assertIn("evidence", gone)
        self.assertEqual(yw_candidates.candidates(book, 20, {}, pdf, {})["verdict"], "no-counterpart")


class ReleaseRulesTests(unittest.TestCase):
    """Validator rules for covers, unmapped hints and reassembled editions."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="r4-test-"))
        self.fx = Fixture(self.tmp / "in")
        plan = json.loads((self.fx.root / "plan.json").read_text())
        by = {e["edition_id"]: e for e in plan["editions"]}
        cur = by[f"cn-{SID_B}-{SHA_B[:12]}"]
        # B's platform file turns out to be another edition: a reassembled edition answers for the book
        self.sha_r = "7" * 64
        self.rid = f"cn-{SID_B}-{self.sha_r[:12]}"
        r = edition(self.rid, SID_B, self.sha_r, KEY_B, basis="reassembled:legacy-images",
                    sid_basis="identifier:jc-content-id", stage="高中", subject="物理", title="物理乙", current=False)
        r.update({"work_id": cur["work_id"], "provenance": "reassembled-from-legacy-images", "metadata_basis": "jc-catalog",
                  "newer_edition_id": cur["edition_id"], "coexists_reason": "kept"})
        r["pdf"].update({"has_text_layer": False, "pages": 12, "text_pages": 0, "ocr_needed_pages": 12})
        r["source"]["id_basis"] = "reassembled-legacy-images"
        r["legacy_spec"] = {"mode": "reassembled", "legacy_pages": 12, "sun_book_key": KEY_B}
        cur.update({"supersedes_edition_id": self.rid, "coexists_reason": "kept"})
        cur["legacy"].update({"sun_book_key": None, "jc_short_key": None, "basis": None,
                              "alias_candidate": {"sun_book_key": KEY_B, "jc_short_key": jc_short_key(KEY_B), "basis": "id"}})
        plan["editions"].append(r)
        write_json(self.fx.root / "plan.json", plan)
        recs = [{"pdf_page": p, "chars": 0, "kind": "ocr_needed", "text": "", "margin": {"top": [], "bottom": []}}
                for p in range(1, 13)]
        write_json(self.fx.pm / f"{self.rid}.json", page_map.build_page_map(
            self.rid, self.sha_r, {"extractor_version": "t", "page_texts": recs}, r["legacy_spec"]))
        side = self.tmp / "side"
        write_json(side / f"{self.sha_r}.json", {
            "sha256": self.sha_r, "pages": 12, "jc_short_key": jc_short_key(KEY_B),
            "page_sources": [{"pdf_page": i + 1, "legacy_image_index": i, "source_sha256": f"{i:064x}"} for i in range(12)]})
        self.release = self.fx.build(self.tmp / "out", "--reassembly", str(side))
        self.hold = {**HOLD, self.sha_r: {}}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def check(self, name, fn, needle):
        path, sums = self.release / name, self.release / "SHA256SUMS"
        before = (path.read_bytes(), sums.read_bytes())
        doc = json.loads(path.read_text())
        fn(doc)
        path.write_text(dumps(doc), encoding="utf-8")
        resum(self.release)
        errs = validate_release.validate(self.release, self.hold)
        path.write_bytes(before[0])
        sums.write_bytes(before[1])
        self.assertTrue(any(needle in e for e in errs), f"{needle!r} not in {errs}")

    def ed(self, doc, eid):
        return next(e for e in doc["editions"] if e["edition_id"] == eid)

    def test_release_with_a_reassembled_edition_is_valid(self):
        self.assertEqual(validate_release.validate(self.release, self.hold), [])
        cat = json.loads((self.release / "catalog.json").read_text())
        r = self.ed(cat, self.rid)
        self.assertEqual((r["corpus"]["relation"], r["corpus"]["text_identity"], r["corpus"]["coverage"]),
                         ("legacy-image-source", "legacy-images", 1.0))
        self.assertEqual((r["preview_default"], r["platform_current"], r["legacy"]["unmapped_pages"]), (False, False, 0))
        self.assertEqual(r["reassembly"], f"reassembly/{self.rid}.json")
        self.assertTrue((self.release / r["reassembly"]).is_file())
        rb = {x["sun_book_key"]: x for x in cat["reader_books"]}[KEY_B]
        self.assertEqual((rb["edition_id"], rb["candidate_edition_ids"]), (self.rid, [f"cn-{SID_B}-{SHA_B[:12]}"]))
        self.assertTrue(self.ed(cat, f"cn-{SID_B}-{SHA_B[:12]}")["preview_default"])       # the current file stays default
        for w in (240, 480):
            self.assertTrue((self.release / f"covers/{self.rid}-{w}.webp").is_file())
        sums = (self.release / "SHA256SUMS").read_text()
        self.assertIn(f"covers/{self.rid}-240.webp", sums)
        self.assertIn(f"reassembly/{self.rid}.json", sums)

    def test_reassembled_rules(self):
        pm = f"page-maps/{self.rid}.json"
        self.check(pm, lambda d: d["pages"][3].update(legacy_basis="image-match"), "must map every page to legacy index pdf_page - 1")
        self.check("catalog.json", lambda d: self.ed(d, self.rid)["pdf"].update(has_text_layer=True), "has no text layer")
        self.check("catalog.json", lambda d: self.ed(d, self.rid).update(provenance="platform-current"),
                   "must go together")
        self.check("catalog.json", lambda d: self.ed(d, self.rid).update(reassembly=None), "is missing")
        self.check(f"reassembly/{self.rid}.json", lambda d: d["page_sources"].pop(), "does not list one verified source image per page")
        self.check(f"reassembly/{self.rid}.json", lambda d: d.update(sha256="0" * 64), "does not list one verified source image per page")
        self.check("catalog.json", lambda d: self.ed(d, f"cn-{SID_B}-{SHA_B[:12]}").update(reassembly="reassembly/x.json"),
                   "reassembly record on an edition that was not reassembled")

    def test_cover_rules(self):
        self.check("catalog.json", lambda d: self.ed(d, self.rid).update(cover=None), "a held edition needs cover.w240")
        self.check("catalog.json", lambda d: self.ed(d, self.rid)["cover"]["sha256"].update(w240="0" * 64),
                   "does not match the SHA-256 in the record")
        self.check("catalog.json", lambda d: self.ed(d, self.rid)["cover"].update(w480="covers/other.webp"),
                   "a held edition needs cover.w240")
        placeholder = next(e["edition_id"] for e in json.loads((self.release / "catalog.json").read_text())["editions"]
                           if not e["pdf"])
        self.check("catalog.json", lambda d: self.ed(d, placeholder).update(cover={"w240": "x"}),
                   "cover on a record whose bytes are not held here")
        (self.release / f"covers/{self.rid}-480.webp").unlink()
        resum(self.release)
        self.assertTrue(any("is missing from the release" in e for e in validate_release.validate(self.release, self.hold)))

    def test_unmapped_hint_rules(self):
        a_id = next(e["edition_id"] for e in json.loads((self.release / "catalog.json").read_text())["editions"]
                    if e["corpus"]["relation"] == "same-bytes")
        pm = f"page-maps/{a_id}.json"

        def drop_row(d):                      # a row disappears and nothing accounts for its legacy index
            d["pages"][5].update(legacy_image_index=None, legacy_basis=None)
            d["legacy"]["mapped_pages"] -= 1
        self.check(pm, drop_row, "together do not cover legacy indexes")
        self.check(pm, lambda d: d["legacy_unmapped"].append({"legacy_image_index": 3, "nearest_mapped": None}),
                   "both mapped and unmapped")
        self.check(pm, lambda d: d.update(legacy_unmapped=None), "legacy_unmapped is missing")

        def unmapped_with_bad_hint(d, hint, own_page=False):
            d["pages"][5].update(legacy_image_index=None, legacy_basis=None)
            entry = {"legacy_image_index": 5, "nearest_mapped": hint}
            if own_page:
                entry["pdf_page"] = 6
            d["legacy_unmapped"].append(entry)
        self.check(pm, lambda d: unmapped_with_bad_hint(d, {"legacy_image_index": 9, "pdf_page": 10, "distance": 4}),
                   "is not the nearest mapped row")
        self.check(pm, lambda d: unmapped_with_bad_hint(d, {"legacy_image_index": 6, "pdf_page": 7, "distance": 1}),
                   "is not the nearest mapped row")                                     # tie must go to the earlier one
        self.check(pm, lambda d: unmapped_with_bad_hint(d, {"legacy_image_index": 4, "pdf_page": 5, "distance": 1}, True),
                   "carries a page of its own")


if __name__ == "__main__":
    unittest.main()
