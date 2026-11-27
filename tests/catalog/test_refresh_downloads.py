"""Offline tests: the download list rebuilt from the platform's catalog parts (refresh_downloads.py)."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "catalog"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import refresh_downloads  # noqa: E402
import validate_release  # noqa: E402
from common import write_json  # noqa: E402
from test_build_validate import HOLD, SID_A, SID_C, Fixture  # noqa: E402


def uid(n: int) -> str:
    return f"9a9a9a9a-0000-4000-8000-{n:012x}"


def record(sid, title, *, stage="初中", subject="英语", edition="人教版", grade="七年级", volume="上册",
           publisher="人民教育出版社", online="2026-08-01T00:00:00.000+0800", extra_tags=()):
    tags = [("zxxxd", stage), ("zxxxk", subject), ("zxxbb", edition), ("zxxnj", grade), ("zxxcc", volume), *extra_tags]
    return {"id": sid, "title": title, "status": "ONLINE", "online_time": online, "update_time": online,
            "create_time": online, "provider_list": [{"name": publisher}],
            "tag_list": [{"tag_dimension_id": k, "tag_name": v} for k, v in tags if v is not None],
            "ti_items": [{"ti_format": "pdf", "ti_file_flag": "source", "ti_size": "2048"}]}


class RefreshDownloadsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="refresh-test-"))
        self.parent = Fixture(self.tmp / "in").build(self.tmp / "releases")
        self.parts = self.tmp / "parts"; self.parts.mkdir()
        refresh_downloads.MIN_RECORDS = 1
        r = [
            record(SID_A, "语文甲", subject="语文"),                                   # held edition in the parent
            record(uid(1), "（根据2022年版课程标准修订）义务教育教科书·英语七年级上册", online="2026-08-20T00:00:00.000+0800"),
            record(uid(2), "义务教育教科书·英语七年级上册", online="2026-08-01T00:00:00.000+0800"),   # same book, older
            record(uid(3), "（根据2022年版课程标准修订）义务教育教科书·英语七年级上册", volume="下册"),  # truncated title, other volume
            record(uid(4), "义务教育教科书·艺术•音乐七年级上册", subject="艺术·音乐"),
            record(uid(5), "义务教育信息科技课程教学指南 三年级全一册", stage="小学", subject="信息科技"),
            record(uid(6), "义务教育教科书·体育与健康教师用书七年级", subject="体育与健康"),
            record(uid(7), "培智学校义务教育实验教科书·律动 一年级", stage="小学", subject="律动"),
            record(uid(8), "盲校义务教育实验教科书·英语七年级", subject="英语"),
            record(uid(9), "特殊教育读本", stage="特殊教育", subject="语文"),
            record(uid(10), "义务教育教科书·英语（三年级起点）四年级上册", stage="小学（五•四学制）", subject="英语（三年级起点）"),
        ]
        write_json(self.parts / "data_version.json", {"module": "tch_material", "module_version": 42, "urls": "u1,u2"})
        write_json(self.parts / "part_1.json", r[:6])
        write_json(self.parts / "part_2.json", r[6:])
        self.records = r

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, name="out"):
        out = self.tmp / name; out.mkdir()
        self.assertEqual(refresh_downloads.main(["--parent", str(self.parent), "--parts", str(self.parts), "--out", str(out),
                                                 "--stamp", "2026-11-27T05:00:00Z"]), 0)
        (release,) = [p for p in out.iterdir() if p.is_dir() and not p.name.startswith(".")]
        return release

    def test_valid_and_deterministic(self):
        release = self.build()
        self.assertEqual(validate_release.validate(release, HOLD), [])
        self.assertEqual(self.build("again").name, release.name)
        self.assertTrue(release.name.startswith("20261127T050000Z-"))

    def test_rules(self):
        release = self.build()
        rows = {r["source_id"]: r for r in json.loads((release / "downloads.json").read_text())["rows"]}
        self.assertIn(uid(1), rows)                                  # revised edition kept
        self.assertNotIn(uid(2), rows)                               # older record of the same book
        self.assertIn(uid(3), rows)                                  # 下册: different 册次 tag, kept
        self.assertEqual(rows[uid(4)]["subject"], "音乐")             # 艺术·音乐 listed under 音乐
        self.assertEqual(rows[uid(4)]["subject_group"], "艺术·音乐")
        self.assertEqual(rows[uid(5)]["subject"], "信息科技")
        self.assertNotIn(uid(6), rows)                               # 教师用书
        self.assertNotIn(uid(7), rows)                               # 培智 subject outside the first catalog's subjects
        self.assertIn(uid(8), rows)                                  # 盲校 英语 stays (first catalog listed such books)
        self.assertNotIn(uid(9), rows)                               # tagged 特殊教育
        self.assertEqual((rows[uid(10)]["stage"], rows[uid(10)]["subject"]), ("小学", "英语"))
        self.assertEqual(rows[uid(1)]["publisher"], "人民教育出版社")
        self.assertEqual(rows[uid(1)]["size_bytes"], 2048)
        self.assertEqual(rows[uid(1)]["sources"], [{"kind": "upstream", "url": refresh_downloads.UPSTREAM_PDF + uid(1),
                                                    "edition_id": None, "platform_current": True}])
        # a row of a held edition gets the same sources build_catalog gave it (attribution included)
        parent = {r["source_id"]: r for r in json.loads((self.parent / "downloads.json").read_text())["rows"]}
        self.assertEqual(rows[SID_A]["sources"], parent[SID_A]["sources"])
        self.assertEqual(rows[SID_A]["edition_id"], parent[SID_A]["edition_id"])
        refresh = json.loads((release / "RELEASE.json").read_text())["refresh"]
        self.assertEqual(refresh["module_version"], 42)
        self.assertEqual([x["source_id"] for x in refresh["superseded_left_out"]], [uid(2)])
        self.assertIn(SID_C, refresh["diff"]["withdrawn_ids"])        # in the parent, not on the platform, no held file

    def test_parent_files_carried(self):
        release = self.build()
        for p in self.parent.rglob("*"):
            rel = p.relative_to(self.parent)
            if p.is_file() and str(rel) not in ("catalog.json", "downloads.json", "RELEASE.json", "SHA256SUMS"):
                self.assertEqual((release / rel).read_bytes(), p.read_bytes(), rel)

    def test_too_few_records_refused(self):
        refresh_downloads.MIN_RECORDS = 100
        with self.assertRaises(SystemExit):
            self.build()


if __name__ == "__main__":
    unittest.main()
