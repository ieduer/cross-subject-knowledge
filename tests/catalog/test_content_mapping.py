"""Offline tests: per-page content proof (corpus database + page images) and image calibration."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "catalog"))
import page_images  # noqa: E402
import page_map  # noqa: E402

try:
    import pymupdf
except ImportError:  # pragma: no cover
    pymupdf = None


def img(pairs, score=0.95, second=0.3):
    return {li: {"pdf_page": p, "score": score, "second": second} for li, p in pairs}


def sentence(i):
    return f"第{i}页独有的内容是关于编号{i * 7919}的故事，主人公名叫角色{i * 104729}，他在城市{i * 1299709}生活。" * 3


class ContentMappingTests(unittest.TestCase):
    def test_every_page_proven_is_aligned(self):
        m = page_map.content_mapping(None, img((k, k + 1) for k in range(50)), 50, 50, informative=50,
                                     same_threshold=0.9)
        self.assertEqual((m["accepted"], m["relation"], m["offset"], m["same_rendering"]), (True, "aligned", 0, True))
        self.assertEqual(m["rows"][1], (0, "image-match", 0.95))
        self.assertEqual(len(m["rows"]), 50)

    def test_other_printing_is_not_called_the_same_rendering(self):
        m = page_map.content_mapping(None, img(((k, k + 1) for k in range(50)), score=0.85), 50, 50, informative=50,
                                     same_threshold=0.9)
        self.assertEqual((m["relation"], m["same_rendering"]), ("aligned", False))

    def test_offset_may_change_but_only_proven_rows_cross_the_change(self):
        # two pages were inserted after legacy 19: offset 0, then offset 2
        pairs = [(k, k + 1) for k in range(0, 20, 3)] + [(k, k + 3) for k in range(24, 60, 3)]
        m = page_map.content_mapping(None, img(pairs), 62, 60, informative=60)
        self.assertTrue(m["accepted"])
        self.assertIsNone(m["offset"])
        self.assertEqual(m["offset_histogram"]["0"] > 0 and m["offset_histogram"]["2"] > 0, True)
        self.assertEqual(m["offset_changes"][0]["after_legacy_index"], 18)
        self.assertEqual(m["rows"][2], (1, "between-anchors", None))        # same offset on both sides
        self.assertTrue(all(p not in m["rows"] for p in range(20, 27)))       # nothing inferred across the change
        self.assertEqual(m["rows"][27], (24, "image-match", 0.95))
        self.assertEqual(m["relation"], "partial")

    def test_gap_rule_applies_to_content_rows(self):
        pairs = [(k, k + 1) for k in range(0, 21, 4)] + [(k, k + 1) for k in range(38, 60, 3)]
        m = page_map.content_mapping(None, img(pairs), 60, 60, informative=60)
        self.assertEqual(m["wide_gaps"], [{"from_legacy_index": 20, "to_legacy_index": 38, "unfilled_rows": 17}])
        self.assertNotIn(30, m["rows"])
        self.assertEqual(m["rows"][60], (59, "image-match", 0.95))
        self.assertEqual(m["relation"], "partial")

    def test_head_and_tail_fill(self):
        pairs = [(k, k + 1) for k in range(5, 56, 5)]
        m = page_map.content_mapping(None, img(pairs), 60, 60, informative=60)
        self.assertEqual(m["rows"][1], (0, "head-consistent", None))
        self.assertEqual(m["rows"][60], (59, "tail-consistent", None))
        self.assertEqual(m["relation"], "aligned")
        longer = page_map.content_mapping(None, img(pairs), 61, 60, informative=60)   # ends do not coincide
        self.assertNotIn(60, longer["rows"])
        shifted = page_map.content_mapping(None, img((k, k + 2) for k in range(5, 56, 5)), 61, 60, informative=60)
        self.assertNotIn(1, shifted["rows"])                                           # offset 1: head not provable
        self.assertEqual(shifted["rows"][61], (59, "tail-consistent", None))

    def test_image_and_text_must_not_disagree(self):
        image = img((k, k + 1) for k in range(0, 40, 2))
        text = {k: {k + 1} for k in range(0, 40, 4)}
        text[8] = {30}                                                    # the text of legacy 8 sits elsewhere
        m = page_map.content_mapping(text, image, 40, 40, informative=40)
        self.assertEqual(m["image_text_conflict_count"], 1)
        self.assertEqual(m["rows"][1], (0, "image+text", 0.95))
        self.assertEqual(m["rows"][3], (2, "image-match", 0.95))
        self.assertEqual(m["rows"][9][1], "between-anchors")              # not proven itself; neighbours agree
        self.assertEqual(m["proven_by_basis"], {"image+text": 9, "image-match": 10})

    def test_text_alone_proves_a_page_and_two_page_chunks_do_not(self):
        text = {k: {k + 1} for k in range(0, 30, 3)}
        text[31] = {32, 33}
        m = page_map.content_mapping(text, None, 60, 60)
        self.assertEqual(m["rows"][1], (0, "text-anchor", None))
        self.assertNotIn(32, m["rows"])
        self.assertEqual(m["proven_by_basis"], {"text-anchor": 10})

    def test_out_of_order_matches_are_dropped(self):
        pairs = [(k, k + 1) for k in range(0, 40, 2)] + [(21, 5)]
        m = page_map.content_mapping(None, img(pairs), 40, 40, informative=40)
        self.assertEqual(m["non_monotone_dropped"], [21])
        self.assertEqual(m["rows"][5], (4, "image-match", 0.95))

    def test_too_few_proven_pages(self):
        m = page_map.content_mapping(None, img((k, k + 1) for k in range(5)), 60, 60, informative=58)
        self.assertEqual((m["accepted"], m["relation"], m["rows"]), (False, "different-edition", {}))
        m = page_map.content_mapping(None, img([]), 60, 60, informative=3)
        self.assertEqual(m["relation"], "none")

    def test_same_bytes_image_check(self):
        cal = {"match_threshold": 0.8, "margin": 0.1}
        rows = [{"legacy_index": k, "blank": False, "pdf_page": k + 1, "score": 0.93, "second": 0.4} for k in range(9)]
        rows.append({"legacy_index": 9, "blank": True})
        chk = page_map.image_check_same_bytes(rows, cal)
        self.assertEqual((chk["confirmed"], chk["informative_pages"], chk["blank_pages"]), (True, 9, 1))
        rows[3]["pdf_page"] = 7                                            # one image matches another page
        chk = page_map.image_check_same_bytes(rows, cal)
        self.assertEqual((chk["confirmed"], chk["matched_elsewhere"]), (False, 1))
        rows[3].update(pdf_page=4, score=0.5)                              # one unclear page of nine: below 95 %
        self.assertFalse(page_map.image_check_same_bytes(rows, cal)["confirmed"])


class CorpusDatabaseTests(unittest.TestCase):
    def test_page_texts_come_from_the_database_and_anchor_every_page(self):
        tmp = Path(tempfile.mkdtemp(prefix="corpus-test-"))
        try:
            db = tmp / "c.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE chunks(id INTEGER PRIMARY KEY, book_key TEXT, section INTEGER, text TEXT, source TEXT)")
            texts = [sentence(i) for i in range(1, 41)]                     # pdf pages 1..40
            for li in range(39):                                             # legacy li is pdf page li + 2
                half = len(texts[li + 1]) // 2
                con.execute("INSERT INTO chunks(book_key, section, text, source) VALUES (?,?,?,?)",
                            ("书甲", li, texts[li + 1][:half], "mineru"))
                con.execute("INSERT INTO chunks(book_key, section, text, source) VALUES (?,?,?,?)",
                            ("书甲", li, texts[li + 1][half:], "mineru"))
            con.execute("INSERT INTO chunks(book_key, section, text, source) VALUES ('书乙', 0, '别的书', 'mineru')")
            con.execute("INSERT INTO chunks(book_key, section, text, source) VALUES ('书甲', 0, '高考题', 'gaokao')")
            con.commit()
            con.close()
            pages = page_map.corpus_page_texts(db, "书甲")
            self.assertEqual(sorted(pages), list(range(39)))
            self.assertNotIn("高考题", pages[0])
            self.assertEqual(pages[5], texts[6][:len(texts[6]) // 2] + "\n" + texts[6][len(texts[6]) // 2:])
            located = page_map.text_rows(page_map.build_shingle_index(texts), pages)
            self.assertEqual(located, {li: {li + 2} for li in range(39)})
            m = page_map.content_mapping(located, None, 40, 39)
            self.assertEqual((m["relation"], m["offset"], len(m["rows"])), ("aligned", 1, 39))
            self.assertEqual(page_map.corpus_page_texts(db, "没有这本书"), {})
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class ImageCalibrationTests(unittest.TestCase):
    def rows(self, true, rival, wrong=()):
        out = [{"legacy_index": i, "blank": False, "pdf_page": i + 1, "score": t, "second": r}
               for i, (t, r) in enumerate(zip(true, rival))]
        out += [{"legacy_index": 900 + i, "blank": False, "pdf_page": 1, "score": s, "second": 0.2}
                for i, s in enumerate(wrong)]
        out.append({"legacy_index": 999, "blank": True})
        return out

    def test_rule_comes_from_known_pairs(self):
        cal = page_images.calibrate([self.rows([0.95, 0.90, 0.97, 0.88], [0.30, 0.71, 0.40, 0.85])])
        self.assertEqual((cal["pages"], cal["true_min"], cal["rival_max"]), (4, 0.88, 0.85))
        self.assertEqual((cal["match_threshold"], cal["margin"]), (0.80, 0.10))
        self.assertEqual(cal["true_pairs_accepted"], 3)                      # 0.88 vs 0.85 is ambiguous
        self.assertIsNone(cal["same_threshold"])                             # scores never prove "same printing"
        # a wrong page that would pass the floor raises the threshold above it
        cal = page_images.calibrate([self.rows([0.99, 0.98], [0.3, 0.4], wrong=[0.86])])
        self.assertEqual((cal["best_page_not_the_true_page"], cal["match_threshold"]), (1, 0.88))
        self.assertEqual(cal["true_pairs_accepted"], 2)
        # a weak wrong best (a near-blank page) changes nothing
        cal = page_images.calibrate([self.rows([0.95], [0.3], wrong=[0.4])])
        self.assertEqual((cal["match_threshold"], cal["wrong_best_max_score"]), (0.80, 0.4))
        with self.assertRaises(ValueError):
            page_images.calibrate([[{"legacy_index": 0, "blank": True}]])

    def test_match_needs_score_and_margin(self):
        cal = {"match_threshold": 0.8, "margin": 0.1}
        rows = [{"legacy_index": 0, "blank": False, "pdf_page": 1, "score": 0.92, "second": 0.40},
                {"legacy_index": 1, "blank": False, "pdf_page": 2, "score": 0.79, "second": 0.10},   # too weak
                {"legacy_index": 2, "blank": False, "pdf_page": 3, "score": 0.90, "second": 0.85},   # ambiguous
                {"legacy_index": 3, "blank": True},
                {"legacy_index": 4, "blank": False, "pdf_page": None, "score": None, "second": None}]
        self.assertEqual(page_images.image_matches(rows, cal), {0: {"pdf_page": 1, "score": 0.92, "second": 0.40}})

    def test_blank_pages_carry_no_information(self):
        self.assertIsNone(page_images.normalise(bytes([200]) * page_images.THUMB_N))
        v = page_images.normalise(bytes(range(256)) * 19 + bytes(page_images.THUMB_N - 4864))
        self.assertAlmostEqual(page_images.ncc(v, v), 1.0, places=6)

    @unittest.skipIf(pymupdf is None, "pymupdf not installed")
    def test_rendered_pages_find_their_own_position(self):
        tmp = Path(tempfile.mkdtemp(prefix="thumb-test-"))
        try:
            doc = pymupdf.open()
            for i in range(12):
                page = doc.new_page(width=420, height=595)
                if i == 7:
                    continue                                                  # a blank page
                for k in range(6):
                    page.insert_text((40 + 13 * ((i * 7 + k * 3) % 17), 60 + 80 * k), f"Sheet {i} block {k} " * 3,
                                     fontsize=10 + (i + k) % 9)
                page.draw_rect(pymupdf.Rect(30 + 25 * i, 300, 90 + 25 * i, 360 + 10 * i), fill=(0, 0, 0))
            path = tmp / "a.pdf"
            doc.save(str(path))
            doc.close()
            meta = page_images.pdf_thumbs(path, "a" * 64, tmp / "pdf")
            self.assertEqual(meta["pages"], 12)
            again = page_images.pdf_thumbs(tmp / "missing.pdf", "a" * 64, tmp / "pdf")   # cached: file not reopened
            self.assertEqual(again, meta)
            thumbs = page_images.load_thumbs(tmp / "pdf", "a" * 64)
            self.assertIsNone(thumbs[7])
            rows = page_images.score_book(thumbs, thumbs, window=4)
            self.assertTrue(rows[7]["blank"])
            informative = [r for r in rows if not r.get("blank")]
            self.assertTrue(all(r["pdf_page"] == r["legacy_index"] + 1 and r["score"] > 0.999 for r in informative))
            cal = page_images.calibrate([rows])
            self.assertEqual(len(page_images.image_matches(rows, cal)), 11)
            # the same pages shifted by one sheet are found at the shifted position
            shifted = page_images.score_book(thumbs[1:], thumbs, window=4)
            self.assertTrue(all(r["pdf_page"] == r["legacy_index"] + 2 for r in shifted if not r.get("blank")))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_manifest_parsing(self):
        tmp = Path(tempfile.mkdtemp(prefix="manifest-test-"))
        try:
            (tmp / "m").write_text("aa11  ./k1/p0.webp\nbb22  ./k1/p1.webp\n")
            self.assertEqual(page_images.load_manifest(tmp / "m"), {"k1/p0.webp": "aa11", "k1/p1.webp": "bb22"})
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
