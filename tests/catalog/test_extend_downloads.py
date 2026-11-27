"""Offline tests: r51 download-list extension (metadata only, source links only)."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "catalog"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import extend_downloads  # noqa: E402
import validate_release  # noqa: E402
from common import write_json  # noqa: E402
from test_build_validate import HOLD, SID_A, Fixture  # noqa: E402

IT = "55555555-5555-4555-8555-555555555555"
MUSIC = "66666666-6666-4666-8666-666666666666"
DANCE = "77777777-7777-4777-8777-777777777777"
BLIND = "88888888-8888-4888-8888-888888888888"


def group(rows, at):
    return {"retrieved_at": at, "headers": {"x-jks-catalog-version": "1"}, "body": rows}


class ExtendDownloadsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="extend-test-"))
        self.parent = Fixture(self.tmp / "in").build(self.tmp / "releases")
        self.extra = self.tmp / "extra"
        self.extra.mkdir()
        write_json(self.extra / "高中__信息技术.json", group([
            {"id": IT, "title": "普通高中教科书·信息技术必修1 数据与计算", "edition": "人教版", "publisher": "人民教育出版社",
             "cover_url": f"https://jks-ai.bdfz.net/cover/{IT}"},
            {"id": SID_A, "title": "语文甲", "edition": "", "publisher": "", "cover_url": ""}], "2026-11-26T10:00:00Z"))
        # the same book in the umbrella 艺术 group and in 音乐: the title names 音乐
        write_json(self.extra / "初中__艺术.json", group([
            {"id": MUSIC, "title": "义务教育教科书·艺术•音乐 七年级上册", "edition": "", "publisher": "", "cover_url": ""},
            {"id": DANCE, "title": "义务教育教科书 舞蹈 七年级上册", "edition": "", "publisher": "", "cover_url": ""}],
            "2026-11-26T10:00:01Z"))
        write_json(self.extra / "初中__音乐.json", group([
            {"id": MUSIC, "title": "义务教育教科书·艺术•音乐 七年级上册", "edition": "", "publisher": "", "cover_url": ""},
            {"id": BLIND, "title": "盲校义务教育实验教科书 音乐 七年级", "edition": "", "publisher": "", "cover_url": ""}],
            "2026-11-26T10:00:02Z"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_extend(self, out_name="out"):
        out = self.tmp / out_name
        out.mkdir(exist_ok=True)
        rc = extend_downloads.main(["--parent", str(self.parent), "--extra", str(self.extra), "--out", str(out)])
        self.assertEqual(rc, 0)
        (release,) = [p for p in out.iterdir() if p.is_dir() and not p.name.startswith(".")]
        return release

    def test_extended_release_is_valid_and_deterministic(self):
        release = self.run_extend()
        self.assertEqual(validate_release.validate(release, HOLD), [])
        self.assertEqual(self.run_extend("again").name, release.name)
        self.assertTrue(release.name.startswith("20261126T100002Z-"))

    def test_rows_subjects_and_sources(self):
        release = self.run_extend()
        rows = {r["source_id"]: r for r in json.loads((release / "downloads.json").read_text())["rows"]}
        parent_rows = json.loads((self.parent / "downloads.json").read_text())["rows"]
        self.assertEqual(len(rows), len(parent_rows) + 3)          # IT, MUSIC, DANCE; BLIND left out
        self.assertNotIn(BLIND, rows)
        self.assertEqual(rows[SID_A]["subject"], "语文")             # already listed: kept as it was
        self.assertEqual(rows[IT]["subject"], "信息技术")
        self.assertEqual(rows[MUSIC]["subject"], "音乐")
        self.assertEqual(rows[DANCE]["subject"], "艺术")
        for sid in (IT, MUSIC, DANCE):
            self.assertIsNone(rows[sid]["edition_id"])
            self.assertEqual(rows[sid]["sources"], [{"kind": "upstream", "url": f"https://jks-ai.bdfz.net/pdf/{sid}",
                                                     "edition_id": None, "platform_current": True}])
        release_doc = json.loads((release / "RELEASE.json").read_text())
        self.assertEqual(release_doc["derived_from"]["release_id"], self.parent.name)
        self.assertEqual(release_doc["extension"]["special_education_rows_left_out"], 1)

    def test_parent_files_carried_over(self):
        release = self.run_extend()
        for p in self.parent.rglob("*"):
            rel = p.relative_to(self.parent)
            if p.is_file() and str(rel) not in ("catalog.json", "downloads.json", "RELEASE.json", "SHA256SUMS"):
                self.assertEqual((release / rel).read_bytes(), p.read_bytes(), rel)
        old = json.loads((self.parent / "catalog.json").read_text())
        new = json.loads((release / "catalog.json").read_text())
        self.assertEqual({**old, "release_id": None}, {**new, "release_id": None})

    def test_subject_name_corrections(self):
        self.assertEqual(extend_downloads.clean_subject({"subject_group": "思想政治", "title": "义务教育教科书·道德与法治七年级上册"}), "道德与法治")
        self.assertEqual(extend_downloads.clean_subject({"subject_group": "思想政治", "title": "普通高中教科书·思想政治必修1"}), "思想政治")
        self.assertEqual(extend_downloads.clean_subject({"subject_group": "生物", "title": "生物学七年级上册"}), "生物学")
        self.assertEqual(extend_downloads.pick_subject("必修3 艺术与科学", ["科学", "艺术"]), "艺术")


if __name__ == "__main__":
    unittest.main()
