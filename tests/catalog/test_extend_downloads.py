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
        self.assertEqual({**old, "release_id": None, "generated_at": None}, {**new, "release_id": None, "generated_at": None})
        self.assertEqual(new["generated_at"], "2026-11-26T10:00:02Z")
        self.assertEqual(json.loads((release / "RELEASE.json").read_text())["generated_at"], "2026-11-26T10:00:02Z")

    def test_subject_name_corrections(self):
        self.assertEqual(extend_downloads.clean_subject({"subject_group": "思想政治", "title": "义务教育教科书·道德与法治七年级上册"}), "道德与法治")
        self.assertEqual(extend_downloads.clean_subject({"subject_group": "思想政治", "title": "普通高中教科书·思想政治必修1"}), "思想政治")
        self.assertEqual(extend_downloads.clean_subject({"subject_group": "生物", "title": "生物学七年级上册"}), "生物学")
        self.assertEqual(extend_downloads.pick_subject("必修3 艺术与科学", ["科学", "艺术"]), "艺术")


class ExtendDownloadsDetailsTests(unittest.TestCase):
    """r52: details records name the publisher, drop OFFLINE records and keep one current record per book."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="extend-details-test-"))
        self.parent = Fixture(self.tmp / "in").build(self.tmp / "releases")
        self.extra, self.details = self.tmp / "extra", self.tmp / "details"
        self.extra.mkdir(); self.details.mkdir()
        ids = {k: f"{n}{n}{n}{n}{n}{n}{n}{n}-{n}{n}{n}{n}-4{n}{n}{n}-8{n}{n}{n}-{n * 12}" for k, n in
               (("old", "a"), ("new", "b"), ("revised", "c"), ("offline", "d"), ("jianpu", "e"), ("platform", "f"))}
        self.ids = ids
        rows = [
            {"id": ids["old"], "title": "义务教育教科书·音乐（五线谱）九年级上册", "publisher": "人民教育出版社"},
            {"id": ids["revised"], "title": "（根据2022年版课程标准修订）义务教育教科书·音乐（五线谱）九年级上册", "publisher": "人民教育出版社"},
            {"id": ids["jianpu"], "title": "（根据2022年版课程标准修订）义务教育教科书·音乐（简谱）九年级上册", "publisher": "人民教育出版社"},
            {"id": ids["offline"], "title": "义务教育教科书·音乐（五线谱）八年级上册", "publisher": "人民教育出版社"},
            {"id": ids["platform"], "title": "义务教育教科书·音乐八年级下册", "publisher": "智慧中小学"},
            {"id": ids["new"], "title": "（根据2022年版课程标准修订）义务教育教科书•艺术•舞蹈九年级上册", "publisher": "人民教育出版社"},
        ]
        write_json(self.extra / "初中__音乐.json", group([{**r, "edition": "人教版", "cover_url": ""} for r in rows], "2026-11-26T10:00:00Z"))
        def det(sid, *, cc, status="ONLINE", online="2024-01-01T00:00:00.000+0800", provider="人民教育出版社"):
            write_json(self.details / f"{sid}.json", {"body": {"status": status, "online_time": online, "update_time": online,
                "provider_list": [{"name": provider}], "tag_list": [{"tag_dimension_id": "zxxbb", "tag_name": "人教版"},
                {"tag_dimension_id": "zxxnj", "tag_name": "九年级"}, {"tag_dimension_id": "zxxcc", "tag_name": cc},
                {"tag_dimension_id": "zxxxk", "tag_name": "音乐"}],
                "ti_items": [{"ti_format": "pdf", "ti_file_flag": "source", "ti_size": "1000"}]}})
        det(ids["old"], cc="九年级上册", online="2025-06-01T00:00:00.000+0800")       # online later, but not revised
        det(ids["revised"], cc="九年级上册", online="2024-07-01T00:00:00.000+0800")
        det(ids["jianpu"], cc="九年级上册")
        det(ids["offline"], cc="八年级上册", status="OFFLINE")
        det(ids["platform"], cc="八年级下册", provider="湖南文艺出版社")
        det(ids["new"], cc="九年级上册")                                         # same tags as the 音乐 books, another book

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_publisher_offline_and_current_record(self):
        out = self.tmp / "out"; out.mkdir()
        self.assertEqual(extend_downloads.main(["--parent", str(self.parent), "--extra", str(self.extra), "--out", str(out),
                                                "--details", str(self.details)]), 0)
        (release,) = [p for p in out.iterdir() if p.is_dir() and not p.name.startswith(".")]
        self.assertEqual(validate_release.validate(release, HOLD), [])
        rows = {r["source_id"]: r for r in json.loads((release / "downloads.json").read_text())["rows"]}
        ids = self.ids
        self.assertIn(ids["revised"], rows)                     # the revised edition is the current record
        self.assertNotIn(ids["old"], rows)                      # superseded, although online later
        self.assertIn(ids["jianpu"], rows)                      # 简谱 and 五线谱 are different books
        self.assertNotIn(ids["offline"], rows)                  # nothing to download
        self.assertIn(ids["new"], rows)                         # same platform tags, different book (舞蹈)
        self.assertEqual(rows[ids["platform"]]["publisher"], "湖南文艺出版社")
        self.assertEqual(rows[ids["platform"]]["publisher_listed"], "智慧中小学")
        self.assertEqual(rows[ids["revised"]]["size_bytes"], 1000)
        ext = json.loads((release / "RELEASE.json").read_text())["extension"]
        self.assertEqual([x["source_id"] for x in ext["superseded_left_out"]], [ids["old"]])
        self.assertEqual([x["source_id"] for x in ext["offline_left_out"]], [ids["offline"]])
        self.assertEqual(ext["added_rows"], 4)
        for name in ("downloads.json", "RELEASE.json"):
            self.assertNotIn("最新", (release / name).read_text())


if __name__ == "__main__":
    unittest.main()
