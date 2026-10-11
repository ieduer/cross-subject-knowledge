"""Offline tests: PDF inspection (synthetic PDFs), folio verification, legacy anchor mapping."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "catalog"))
import inspect_pdf  # noqa: E402
import page_map  # noqa: E402

try:
    import pymupdf
except ImportError:  # pragma: no cover
    pymupdf = None


def rec(pdf_page, folio=None, where="bottom", body="lorem ipsum dolor sit amet"):
    """A page record as produced by inspect_pdf (text + position-true margin strips)."""
    margin = {"top": [], "bottom": []}
    text = body
    if folio is not None:
        if where == "head":
            text = f"{folio}\n{body}"
        else:
            margin[where] = [f"{folio} 第一单元"]
    return {"pdf_page": pdf_page, "chars": len(text), "kind": "text", "text": text, "margin": margin}


def book(n, front, folio_of=lambda p, front: p - front, skip=lambda p: False, where="bottom"):
    return [rec(p, None if p <= front or skip(p) else folio_of(p, front), where) for p in range(1, n + 1)]


def sentence(i):
    return f"第{i}页独有的内容是关于编号{i * 7919}的故事，主人公名叫角色{i * 104729}，他在城市{i * 1299709}生活。"


class FolioTests(unittest.TestCase):
    def test_candidates(self):
        c = page_map.folio_candidates
        self.assertEqual(c(["１４第十九章二次根式"]), {14})          # full-width digits, leading
        self.assertEqual(c(["第一章 机械运动 15"]), {15})            # trailing
        self.assertEqual(c(["古诗词诵读105"]), {105})
        self.assertEqual(c(["4 古代诗歌四首 阅读 15"]), {4, 15})
        self.assertEqual(c(["图 2-13 示意", "2024年版", "1234"]), set())

    def test_clean_book_is_accepted(self):
        res = page_map.verify_folios(book(120, 6))
        self.assertTrue(res["verified"])
        self.assertEqual(res["segments"], [{"start_pdf_page": 7, "offset": 6}])
        labels = res["labels"]
        self.assertEqual(labels[5], {"printed_label": None, "printed_verified": False})   # front matter
        self.assertEqual(labels[6], {"printed_label": "1", "printed_verified": True})
        self.assertEqual(labels[119], {"printed_label": "114", "printed_verified": True})

    def test_head_zone_as_in_keben(self):
        res = page_map.verify_folios(book(100, 4, where="head"))
        self.assertTrue(res["verified"])
        self.assertEqual(res["zone"], "head")

    def test_undetected_pages_inside_the_run_are_labelled_but_not_verified(self):
        pages = book(100, 4, skip=lambda p: p % 10 == 0)
        res = page_map.verify_folios(pages)
        self.assertTrue(res["verified"])
        self.assertEqual(res["labels"][49], {"printed_label": "46", "printed_verified": False})
        self.assertEqual(res["labels"][50], {"printed_label": "47", "printed_verified": True})

    def test_page_showing_a_different_number_gets_no_label(self):
        pages = book(100, 4)
        pages[49] = rec(50, 77)            # pdf page 50 should print 46 but 77 was read there
        res = page_map.verify_folios(pages)
        self.assertTrue(res["verified"])
        self.assertEqual(res["labels"][49], {"printed_label": None, "printed_verified": False})
        self.assertEqual(res["labels"][48]["printed_label"], "45")
        self.assertEqual(res["labels"][50]["printed_label"], "47")

    def test_nothing_is_asserted_after_the_last_verified_page(self):
        pages = book(100, 4, skip=lambda p: p > 95)
        res = page_map.verify_folios(pages)
        self.assertTrue(res["verified"])
        self.assertEqual(res["labels"][94]["printed_label"], "91")
        self.assertIsNone(res["labels"][97]["printed_label"])

    def test_mid_book_shift_is_rejected(self):
        # three unnumbered plates inserted after pdf page 60: folio = p-6, then p-9
        pages = book(120, 6, folio_of=lambda p, f: p - f if p <= 60 else p - f - 3)
        res = page_map.verify_folios(pages)
        self.assertFalse(res["verified"])
        self.assertTrue(all(l["printed_label"] is None and not l["printed_verified"] for l in res["labels"]))
        self.assertIn("rate", res["reason"])

    def test_sparse_detection_is_rejected(self):
        pages = book(120, 6, skip=lambda p: p % 3 != 0)   # folio readable on a third of the pages
        res = page_map.verify_folios(pages)
        self.assertFalse(res["verified"])
        self.assertIn("coverage", res["reason"])
        self.assertTrue(all(l["printed_label"] is None for l in res["labels"]))

    def test_folios_that_stop_early_are_rejected(self):
        pages = book(120, 6, skip=lambda p: p > 90)
        res = page_map.verify_folios(pages)
        self.assertFalse(res["verified"])
        self.assertIn("end", res["reason"])

    def test_documented_restart_is_accepted_and_recorded(self):
        # part two starts again at folio 1 on pdf page 71
        pages = book(130, 6, folio_of=lambda p, f: p - f if p <= 70 else p - 70)
        res = page_map.verify_folios(pages)
        self.assertTrue(res["verified"])
        self.assertEqual(res["restarts"], 1)
        self.assertEqual(res["segments"][1], {"start_pdf_page": 71, "offset": 70})
        self.assertEqual(res["labels"][70]["printed_label"], "1")
        self.assertEqual(res["labels"][69]["printed_label"], "64")

    def test_book_without_text_gets_no_labels(self):
        pages = [{"pdf_page": p, "chars": 0, "kind": "ocr_needed", "text": "", "margin": {"top": [], "bottom": []}}
                 for p in range(1, 41)]
        res = page_map.verify_folios(pages)
        self.assertFalse(res["verified"])
        self.assertEqual(res["reason"], "no-folio-candidates")


class LegacyAnchorTests(unittest.TestCase):
    def setUp(self):
        self.texts = [sentence(i) * 3 for i in range(1, 101)]          # pdf pages 1..100
        self.index = page_map.build_shingle_index(self.texts)

    def hits(self, pairs):
        # (legacy_index, pdf_page): the corpus text of that legacy page is the text of that pdf page
        return [{"legacy_index": li, "text": self.texts[p - 1][:120]} for li, p in pairs]

    def test_locate_is_unique_or_nothing(self):
        self.assertEqual(page_map.locate_text(self.index, self.texts[41])["pdf_page"], 42)
        self.assertIsNone(page_map.locate_text(self.index, "这段文字在任何一页上都不存在，完全无关的内容。" * 3)["pdf_page"])
        twin = page_map.build_shingle_index([sentence(5) * 3, sentence(5) * 3])
        self.assertIsNone(page_map.locate_text(twin, sentence(5) * 3)["pdf_page"])     # ambiguous -> not an anchor

    def test_chunk_running_onto_the_next_page_is_admissible_on_both(self):
        # a chunk that begins at the foot of page 41 and mostly sits on page 42 is filed by the
        # corpus under either page: it must not decide between them
        chunk = self.texts[40][-40:] + self.texts[41][:150]
        loc = page_map.locate_text(self.index, chunk)
        self.assertEqual((loc["pdf_page"], loc["alt_page"], loc["lead_in"]), (42, 41, True))
        # text scattered over distant pages is not an anchor
        mixed = self.texts[10][:90] + self.texts[70][:80]
        self.assertIsNone(page_map.locate_text(self.index, mixed)["pdf_page"])

    def test_two_page_chunk_is_compatible_with_either_filing_but_never_decides(self):
        firm = [(k, k + 1) for k in (3, 10, 17, 25, 33, 48, 61, 77, 90)]
        chunk = self.texts[40][-40:] + self.texts[41][:150]                 # admissible on 41 and 42
        for legacy in (40, 41):                                             # filed under either page
            hits = self.hits(firm) + [{"legacy_index": legacy, "text": chunk}]
            m = page_map.legacy_mapping(page_map.collect_anchors(self.index, hits), 100, 100)
            self.assertTrue(m["accepted"], legacy)
            self.assertEqual((m["offset"], m["anchors"], m["two_page_anchors"]), (0, 9, 1))
        hits = self.hits(firm) + [{"legacy_index": 45, "text": chunk}]      # neither page fits
        m = page_map.legacy_mapping(page_map.collect_anchors(self.index, hits), 100, 100)
        self.assertFalse(m["accepted"])
        self.assertEqual((m["reason"], m["incompatible_two_page_anchors"]), ("contradictory-anchors", 1))
        # two-page chunks alone are never enough
        only = [{"legacy_index": k, "text": self.texts[k][-40:] + self.texts[k + 1][:150]} for k in range(5, 95, 9)]
        m = page_map.legacy_mapping(page_map.collect_anchors(self.index, only), 100, 100)
        self.assertFalse(m["accepted"])
        self.assertEqual(m["reason"], "too-few-anchors")

    def test_anchors_that_stop_early_prove_a_prefix_only(self):
        # same page count is not enough: nothing after legacy page 40 was compared
        pairs = [(k, k + 1) for k in (2, 6, 10, 14, 18, 22, 26, 30, 34, 40)]
        m = page_map.legacy_mapping(page_map.collect_anchors(self.index, self.hits(pairs)), 100, 100)
        self.assertTrue(m["accepted"])
        self.assertFalse(m["reaches_end"])
        self.assertEqual(m["relation"], "partial")
        self.assertEqual(m["rows"][1], (0, "head-consistent"))
        self.assertEqual(m["rows"][41], (40, "anchor"))
        self.assertNotIn(42, m["rows"])
        self.assertNotIn(100, m["rows"])

    def test_consistent_plus_one_is_accepted_and_recorded(self):
        # the PDF has one extra leading page: legacy index k is pdf page k + 2
        pairs = [(k, k + 2) for k in (3, 10, 17, 25, 33, 48, 61, 77, 90)]
        info = page_map.collect_anchors(self.index, self.hits(pairs))
        m = page_map.legacy_mapping(info, pdf_pages=100, legacy_pages=99)
        self.assertTrue(m["accepted"])
        self.assertEqual((m["offset"], m["relation"], m["offset_histogram"]), (1, "aligned", {"1": 9}))
        self.assertEqual(m["rows"][5], (3, "anchor"))
        self.assertEqual(m["rows"][6], (4, "between-anchors"))
        self.assertNotIn(1, m["rows"])                                  # the extra page has no legacy image
        self.assertNotIn(2, m["rows"])      # before the first anchor with offset != 0: not provable
        self.assertEqual(m["rows"][100], (98, "tail-consistent"))       # 100 - 1 == 99: ends coincide

    def test_offset_zero_extends_to_the_first_page_but_not_past_a_longer_tail(self):
        # same origin, the PDF has one extra page at the end (the pattern found in real books)
        pairs = [(k, k + 1) for k in (10, 17, 25, 33, 48, 61, 77, 90)]
        m = page_map.legacy_mapping(page_map.collect_anchors(self.index, self.hits(pairs)), 100, 99)
        self.assertTrue(m["accepted"])
        self.assertEqual(m["offset"], 0)
        self.assertEqual(m["rows"][1], (0, "head-consistent"))
        self.assertEqual(m["rows"][50], (49, "between-anchors"))
        self.assertNotIn(95, m["rows"])      # after the last anchor, ends do not coincide
        self.assertNotIn(100, m["rows"])

    def test_edges_stay_null_when_page_counts_do_not_fit(self):
        pairs = [(k, k + 2) for k in (10, 17, 25, 33, 48, 61, 77, 90)]
        m = page_map.legacy_mapping(page_map.collect_anchors(self.index, self.hits(pairs)), 100, 97)
        self.assertTrue(m["accepted"])
        self.assertFalse(m["tail_consistent"])
        self.assertNotIn(5, m["rows"])       # before the first anchor
        self.assertNotIn(95, m["rows"])      # after the last anchor
        self.assertIn(50, m["rows"])

    def test_contradictory_anchors_are_rejected(self):
        pairs = [(k, k + 2) for k in (3, 10, 17, 25, 33, 48, 61, 77)] + [(90, 95)]
        m = page_map.legacy_mapping(page_map.collect_anchors(self.index, self.hits(pairs)), 100, 99)
        self.assertFalse(m["accepted"])
        self.assertEqual(m["reason"], "contradictory-anchors")
        self.assertEqual(m["rows"], {})
        self.assertEqual(m["relation"], "different-edition")   # the pagination differs somewhere

    def test_two_supported_offsets_mean_a_different_edition(self):
        pairs = [(k, k + 2) for k in (3, 10, 17, 25, 33)] + [(k, k + 5) for k in (48, 61, 77, 90)]
        m = page_map.legacy_mapping(page_map.collect_anchors(self.index, self.hits(pairs)), 100, 96)
        self.assertFalse(m["accepted"])
        self.assertEqual(m["relation"], "different-edition")
        self.assertEqual(m["offset_histogram"], {"1": 5, "4": 4})

    def test_same_legacy_page_on_two_pdf_pages_contradicts(self):
        pairs = [(k, k + 2) for k in (3, 10, 17, 25, 33, 48, 61, 77, 90)] + [(3, 40)]
        m = page_map.legacy_mapping(page_map.collect_anchors(self.index, self.hits(pairs)), 100, 99)
        self.assertFalse(m["accepted"])
        self.assertEqual(m["split_legacy_pages"], 1)

    def test_gap_of_sixteen_is_filled_and_seventeen_is_not(self):
        base = [(k, k + 1) for k in (2, 10, 18, 26, 34, 42, 50)]
        for width, filled in ((16, True), (17, False)):
            pairs = base + [(50 + width, 51 + width), (90, 91)]
            m = page_map.legacy_mapping(page_map.collect_anchors(self.index, self.hits(pairs)), 100, 100)
            self.assertTrue(m["accepted"])
            self.assertEqual(52 in m["rows"], filled, width)             # first row inside the stretch
            self.assertEqual(m["rows"][51], (50, "anchor"))
            self.assertEqual(m["rows"][51 + width], (50 + width, "anchor"))
            if not filled:
                self.assertEqual(m["wide_gaps"][0], {"from_pdf_page": 51, "to_pdf_page": 68, "unfilled_rows": 16})
                self.assertGreaterEqual(m["rows_nulled_by_gap_rule"], 16)

    def test_insertion_and_removal_inside_a_wide_gap_is_not_silently_accepted(self):
        # A revision adds a page after legacy 45 and drops one after legacy 60: totals are equal and
        # every anchor outside 45..60 still sits at offset 0. Nothing inside the stretch was compared,
        # so none of its rows may be filled and the book may not be called aligned.
        pairs = [(k, k + 1) for k in (4, 10, 16, 22, 28, 34, 40)] + [(k, k + 1) for k in (66, 72, 78, 84, 90, 96)]
        m = page_map.legacy_mapping(page_map.collect_anchors(self.index, self.hits(pairs)), 100, 100)
        self.assertTrue(m["accepted"])
        self.assertTrue(m["reaches_end"])
        self.assertEqual(m["relation"], "partial")
        self.assertEqual(m["wide_gaps"], [{"from_pdf_page": 41, "to_pdf_page": 67, "unfilled_rows": 25}])
        self.assertTrue(all(p not in m["rows"] for p in range(42, 67)))
        self.assertEqual(m["rows"][41], (40, "anchor"))
        self.assertEqual(m["rows"][67], (66, "anchor"))
        self.assertEqual(m["rows"][100], (99, "tail-consistent"))

    def test_head_and_tail_are_filled_only_within_the_gap_limit(self):
        pairs = [(k, k + 1) for k in range(20, 81, 6)]                     # first anchor at 20, last at 80
        m = page_map.legacy_mapping(page_map.collect_anchors(self.index, self.hits(pairs)), 100, 100)
        self.assertTrue(m["accepted"])
        self.assertNotIn(1, m["rows"])                                    # 20 pages before the first anchor
        self.assertNotIn(100, m["rows"])                                  # anchors stop before the last 15 %
        self.assertEqual(m["relation"], "partial")

    def test_too_few_anchors_establish_nothing(self):
        m = page_map.legacy_mapping(page_map.collect_anchors(self.index, self.hits([(3, 4), (9, 10)])), 100, 100)
        self.assertEqual((m["accepted"], m["relation"], m["rows"]), (False, "none", {}))

    def test_fewer_than_eight_anchors_are_not_enough(self):
        pairs = [(k, k + 2) for k in (3, 10, 17, 25, 33, 48, 61)]
        m = page_map.legacy_mapping(page_map.collect_anchors(self.index, self.hits(pairs)), 100, 99)
        self.assertFalse(m["accepted"])
        self.assertEqual(m["reason"], "too-few-anchors")

    def test_same_bytes_requires_equal_page_counts(self):
        text_rec = {"extractor_version": "t", "page_texts": book(30, 2)}
        pm = page_map.build_page_map("cn-x-0000000000000000", "0" * 64, text_rec,
                                     {"mode": "same-bytes", "legacy_pages": 30})
        self.assertEqual([r["legacy_image_index"] for r in pm["pages"]], list(range(30)))
        with self.assertRaises(ValueError):
            page_map.build_page_map("cn-x-0000000000000000", "0" * 64, text_rec,
                                    {"mode": "same-bytes", "legacy_pages": 31})


@unittest.skipIf(pymupdf is None, "pymupdf not installed")
class InspectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="inspect-test-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_pdf(self, name, n=30, front=3, image_only=()):
        doc = pymupdf.open()
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
        pix.clear_with(180)
        for p in range(1, n + 1):
            page = doc.new_page(width=420, height=595)
            if p in image_only:
                page.insert_image(pymupdf.Rect(20, 20, 400, 570), pixmap=pix)
                continue
            for k, word in enumerate(("alpha", "beta", "gamma", "delta")):
                page.insert_text((60, 120 + 20 * k), f"Body text of sheet {p}: {word} line.", fontsize=11)
            if p > front:
                x = 60 if p % 2 else 330
                page.insert_text((x, 570), str(p - front), fontsize=9)
        path = self.tmp / name
        doc.save(str(path))
        doc.close()
        return path

    def test_text_layer_folios_and_cache(self):
        path = self.make_pdf("a.pdf")
        rec_, hit = inspect_pdf.inspect(path, self.tmp / "cache")
        self.assertFalse(hit)
        self.assertEqual((rec_["pages"], rec_["has_text_layer"], rec_["ocr_needed_pages"]), (30, True, 0))
        self.assertIn("Body text of sheet 1", rec_["first_page_text_sample"])
        res = page_map.verify_folios(rec_["page_texts"])
        self.assertTrue(res["verified"])
        self.assertEqual((res["zone"], res["segments"]), ("bottom", [{"start_pdf_page": 4, "offset": 3}]))
        # same hash + extractor version: served from the cache, never extracted twice
        with mock.patch.object(inspect_pdf, "extract", side_effect=AssertionError("re-extracted")):
            again, hit2 = inspect_pdf.inspect(path, self.tmp / "cache")
        self.assertTrue(hit2)
        self.assertEqual(again, rec_)

    def test_watermark_only_pages_are_not_text_pages(self):
        doc = pymupdf.open()
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
        pix.clear_with(180)
        for kind in ("plate", "empty", "body"):
            page = doc.new_page(width=420, height=595)
            page.insert_text((60, 580), "仅供个人学习使用，未经授权不得另做他用", fontname="china-s", fontsize=8)
            if kind == "plate":
                page.insert_image(pymupdf.Rect(20, 20, 400, 500), pixmap=pix)
            if kind == "body":
                page.insert_text((60, 120), "Real body text on this page, long enough.", fontsize=11)
        path = self.tmp / "w.pdf"
        doc.save(str(path))
        doc.close()
        rec_, _ = inspect_pdf.inspect(path, self.tmp / "cache")
        self.assertEqual([p["kind"] for p in rec_["page_texts"]], ["ocr_needed", "blank", "text"])
        self.assertEqual((rec_["ocr_needed_pages"], rec_["blank_pages"], rec_["text_pages"]), (1, 1, 1))
        self.assertIn("仅供个人学习使用", rec_["page_texts"][0]["text"])       # the raw text is kept as read

    def test_image_only_pages_are_ocr_needed_not_invented(self):
        path = self.make_pdf("b.pdf", n=12, image_only=range(1, 13))
        rec_, _ = inspect_pdf.inspect(path, self.tmp / "cache")
        self.assertFalse(rec_["has_text_layer"])
        self.assertEqual((rec_["ocr_needed_pages"], rec_["text_pages"], rec_["total_chars"]), (12, 0, 0))
        self.assertTrue(all(p["text"].strip() == "" for p in rec_["page_texts"]))
        pm = page_map.build_page_map("cn-x-1111111111111111", rec_["sha256"], rec_, {"mode": "none", "reason": "t"})
        self.assertFalse(pm["printed"]["verified"])
        self.assertEqual(pm["ocr"]["ocr_needed_pages"], 12)
        self.assertTrue(all(r["printed_label"] is None and r["legacy_image_index"] is None and r["ocr_page"] is None
                            for r in pm["pages"]))


if __name__ == "__main__":
    unittest.main()
