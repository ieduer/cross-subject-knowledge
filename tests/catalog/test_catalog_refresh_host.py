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


if __name__ == "__main__":
    unittest.main()
