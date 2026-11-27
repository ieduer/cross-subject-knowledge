"""Offline tests: the host job's guards (catalog_refresh_host.guards)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "catalog"))
import catalog_refresh_host as host  # noqa: E402


def rows(n, held=()):
    return [{"source_id": f"s{i}", "sources": [{"kind": "vps"}] if f"s{i}" in held else [{"kind": "upstream"}]} for i in range(n)]


def refresh(added=0, withdrawn_ids=(), before=("语文", "数学"), after=("语文", "数学"), total=2000):
    return {"rows": total, "diff": {"added": added, "withdrawn": len(withdrawn_ids), "withdrawn_ids": list(withdrawn_ids),
                                    "subjects_before": list(before), "subjects_after": list(after)}}


class GuardTests(unittest.TestCase):
    def test_small_change_is_ready(self):
        self.assertEqual(host.guards(rows(2000), refresh(added=30, withdrawn_ids=[f"s{i}" for i in range(40)])), [])

    def test_large_withdrawal_or_addition_is_held(self):
        self.assertTrue(any("withdrawn" in r for r in host.guards(rows(2000), refresh(withdrawn_ids=[f"s{i}" for i in range(101)]))))
        self.assertTrue(any("added" in r for r in host.guards(rows(2000), refresh(added=401))))

    def test_lost_subject_or_held_file_is_held(self):
        self.assertTrue(any("subjects lost" in r for r in host.guards(rows(2000), refresh(after=("语文",)))))
        self.assertTrue(any("held on this site" in r for r in host.guards(rows(2000, held={"s3"}), refresh(withdrawn_ids=["s3"]))))

    def test_too_few_rows_is_held(self):
        self.assertTrue(any("only" in r for r in host.guards(rows(2000), refresh(total=10))))


class CheckFilesTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="refresh-host-test-"))
        self.state, self.get = host.STATE, host.get
        host.STATE, host.CHECK_PAUSE = self.tmp, 0
        self.calls = []

        def fake_get(url, timeout=0):
            self.calls.append(url)
            if "forbidden" in url:
                import urllib.error
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
            return b'{"status": "ONLINE", "ti_items": [{"ti_format": "pdf", "ti_file_flag": "source", "ti_size": "99"}]}'
        host.get = fake_get

    def tearDown(self):
        import shutil
        host.STATE, host.get = self.state, self.get
        shutil.rmtree(self.tmp, ignore_errors=True)

    def item(self, sid):
        return {"id": sid, "title": "义务教育教科书·英语七年级上册", "status": "ONLINE",
                "tag_list": [{"tag_dimension_id": "zxxxd", "tag_name": "初中"}, {"tag_dimension_id": "zxxxk", "tag_name": "英语"}]}

    def test_checks_written_and_reused(self):
        stats = host.check_files([self.item("ok-1"), self.item("forbidden-2")])
        self.assertEqual((stats["fetched"], stats["errors"]), (2, 0))
        ok = host.read_json(self.tmp / "checked" / "ok-1.json")
        self.assertEqual((ok["http_status"], ok["pdf"], ok["size"]), (200, True, 99))
        self.assertEqual(host.read_json(self.tmp / "checked" / "forbidden-2.json")["http_status"], 403)
        n = len(self.calls)
        self.assertEqual(host.check_files([self.item("ok-1"), self.item("forbidden-2")])["cached"], 2)
        self.assertEqual(len(self.calls), n)                        # nothing fetched again within the TTL


if __name__ == "__main__":
    unittest.main()
