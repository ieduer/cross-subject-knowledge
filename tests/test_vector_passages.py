"""r50 vector builders: page-furniture cleaning and passage windows (pure Python; no model, no FAISS)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_primary_vector_index as primary  # noqa: E402
import build_supplemental_vector_index as supplemental  # noqa: E402


class VectorPassageTests(unittest.TestCase):
    def test_cleaning_removes_page_furniture_only(self):
        text = "人民都育出版社\n释迦牟尼创立佛教\n![](images/abc123.jpg)\n$$\nx^2\n$$\n仅供个人学习使用，未经授权不得另做他用\n人民教育出版社出版的书"
        self.assertEqual(primary.prepare_text(text, "strip-page-furniture-v1"), "释迦牟尼创立佛教\n\nx^2\n\n人民教育出版社出版的书")
        self.assertEqual(primary.prepare_text(text, "raw"), text)

    def test_windows_cover_the_whole_text_with_overlap(self):
        text = "".join(chr(0x4E00 + i % 500) for i in range(1300))
        windows = primary.passage_windows(text, 512, 400)
        self.assertEqual([len(w) for w in windows], [512, 512, 500])
        self.assertEqual(windows[0][400:], windows[1][:112])
        self.assertTrue(windows[-1].endswith(text[-20:]))
        self.assertEqual(primary.passage_windows(text[:530], 512, 400), [text[:512]], "under 48 new characters: no extra window")
        self.assertEqual(primary.passage_windows(text, 0, 400), [text])
        self.assertEqual(supplemental.passage_windows(text, 512, 400), windows, "both builders use one rule")

    def test_chunk_passages_repeat_the_chunk_id(self):
        ids, windows = primary.chunk_passages([7, 9], ["甲" * 1000, "乙" * 100], "raw", 512, 400)
        self.assertEqual(ids, [7, 7, 7, 9])
        self.assertEqual(len(windows), 4)


if __name__ == "__main__":
    unittest.main()
