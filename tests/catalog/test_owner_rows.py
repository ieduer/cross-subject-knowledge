"""Offline tests: owner-confirmed legacy -> PDF pairs applied by the builder."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "catalog"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import legacy_coverage  # noqa: E402
import page_map  # noqa: E402
import validate_release  # noqa: E402
from common import dumps, jc_short_key, write_json  # noqa: E402
from test_build_validate import HOLD, KEY_B, SHA_B, SID_B, Fixture, page_recs, resum  # noqa: E402

B_ID = f"cn-{SID_B}-{SHA_B[:12]}"
SHORT_B = jc_short_key(KEY_B)


def row(li, p, **kw):
    return {"jc_short_key": SHORT_B, "pdf_sha256": SHA_B[:12], "legacy_image_index": li, "pdf_page": p,
            "decision": "DECISIONS.md Y2", "date": "2026-10-11", **kw}


class OwnerRowsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="owner-rows-test-"))
        self.fx = Fixture(self.tmp / "in")
        plan = json.loads((self.fx.root / "plan.json").read_text())
        b = next(e for e in plan["editions"] if e["edition_id"] == B_ID)
        b["legacy_spec"] = {"mode": "content", "legacy_pages": 40, "sun_book_key": KEY_B}
        write_json(self.fx.root / "plan.json", plan)
        # automatic mapping proves legacy 0..15 on pdf pages 1..16 only
        img = {k: {"pdf_page": k + 1, "score": 0.95, "second": 0.3} for k in range(16)}
        write_json(self.fx.pm / f"{B_ID}.json", page_map.build_page_map(
            B_ID, SHA_B, {"extractor_version": "t", "page_texts": page_recs(40, 2)},
            {**b["legacy_spec"], "image_rows": img, "informative": 40}))
        self.n = 0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, rows):
        self.n += 1
        f = self.tmp / f"rows-{self.n}.json"
        write_json(f, {"schema": "sun-catalog-v1/owner-confirmed-rows", "version": 1, "rows": rows})
        return self.fx.build(self.tmp / f"out-{self.n}", "--owner-rows", str(f))

    GOOD = [row(20, 22, note="start page"), row(21, 23),
            row(16, 16, shares_pdf_page_with=15, content_revised=True, note="same position, text revised")]

    def test_rows_are_added_flagged_and_counted(self):
        base = self.fx.build(self.tmp / "base")
        release = self.build(self.GOOD)
        self.assertEqual(validate_release.validate(release, HOLD), [])
        pm = json.loads((release / "page-maps" / f"{B_ID}.json").read_text())
        before = json.loads((base / "page-maps" / f"{B_ID}.json").read_text())
        r22, r16 = pm["pages"][21], pm["pages"][15]
        self.assertEqual((r22["legacy_image_index"], r22["legacy_basis"], r22["legacy_content_revised"]),
                         (20, "owner-confirmed", False))
        self.assertEqual(r22["legacy_confirmation"], {"decision": "DECISIONS.md Y2", "date": "2026-10-11", "note": "start page"})
        # two legacy pages on one PDF page: the automatic row stays, the confirmed one is listed beside it
        self.assertEqual((r16["legacy_image_index"], r16["legacy_basis"]), (15, "image-match"))
        self.assertEqual([(a["legacy_image_index"], a["legacy_basis"], a["legacy_content_revised"]) for a in r16["legacy_also"]],
                         [(16, "owner-confirmed", True)])
        self.assertEqual((before["legacy"]["mapped_pages"], pm["legacy"]["mapped_pages"]), (16, 19))
        self.assertEqual(pm["legacy"]["owner_confirmed_rows"], 3)
        unmapped = {u["legacy_image_index"] for u in pm["legacy_unmapped"]}
        self.assertTrue(unmapped.isdisjoint({16, 20, 21}))
        self.assertEqual(len(unmapped), 21)
        # every automatic row is untouched
        changed = [i for i, (a, b) in enumerate(zip(before["pages"], pm["pages"])) if a != b]
        self.assertEqual(changed, [15, 21, 22])
        self.assertEqual({k: v for k, v in pm["pages"][15].items() if k != "legacy_also"}, before["pages"][15])
        cat = json.loads((release / "catalog.json").read_text())
        b = next(e for e in cat["editions"] if e["edition_id"] == B_ID)
        self.assertEqual((b["legacy"]["mapped_pages"], b["legacy"]["unmapped_pages"], b["corpus"]["relation"]), (19, 21, "partial"))
        rel = json.loads((release / "RELEASE.json").read_text())
        self.assertEqual((rel["counts"]["owner_confirmed_rows"], rel["counts"]["legacy_also_rows"]), (3, 1))
        self.assertIn("owner-confirmed-rows", rel["inputs"])
        self.assertEqual(set(legacy_coverage.edition_rows(release, B_ID)) - set(range(16)), {16, 20, 21})

    def test_without_rows_nothing_changes_and_same_rows_give_the_same_release(self):
        a, b = self.build(self.GOOD), self.build(self.GOOD)
        self.assertEqual(a.name, b.name)
        self.assertEqual((a / "SHA256SUMS").read_bytes(), (b / "SHA256SUMS").read_bytes())

    def test_conflicts_stop_the_build(self):
        for bad, why in (([row(5, 30)], "already has a row"),                       # legacy index mapped automatically
                         ([row(20, 10)], "already holds legacy index 9"),            # PDF page taken, not acknowledged
                         ([row(20, 10, shares_pdf_page_with=3)], "already holds legacy index 9"),
                         ([row(20, 22, shares_pdf_page_with=15)], "holds no row"),
                         ([row(20, 22), row(20, 23)], "already has a row"),          # the same legacy index twice
                         ([dict(row(20, 22), pdf_sha256="f" * 12)], "not the PDF this pair was confirmed on"),
                         ([dict(row(20, 22), jc_short_key="000000000000")], "not the PDF this pair was confirmed on"),
                         ([row(20, 99)], "outside the PDF"), ([row(77, 22)], "outside the PDF")):
            with self.assertRaises(SystemExit) as cm:
                self.build(bad)
            self.assertIn(why, str(cm.exception), bad)

    def check(self, release, fn, needle):
        path = release / "page-maps" / f"{B_ID}.json"
        sums = release / "SHA256SUMS"
        before = (path.read_bytes(), sums.read_bytes())
        doc = json.loads(path.read_text())
        fn(doc)
        path.write_text(dumps(doc), encoding="utf-8")
        resum(release)
        errs = validate_release.validate(release, HOLD)
        path.write_bytes(before[0])
        sums.write_bytes(before[1])
        self.assertTrue(any(needle in e for e in errs), f"{needle!r} not in {errs}")

    def test_validator_rules(self):
        release = self.build(self.GOOD)
        self.check(release, lambda d: d["pages"][21].pop("legacy_confirmation"), "needs basis, decision, date")
        self.check(release, lambda d: d["pages"][21].update(legacy_content_revised="yes"), "needs basis, decision, date")
        self.check(release, lambda d: d["pages"][15]["legacy_also"][0].update(legacy_basis="image-match"),
                   "needs basis, decision, date")
        self.check(release, lambda d: d["pages"][3].update(legacy_content_revised=True),
                   "legacy_content_revised on a row that was not confirmed by the owner")
        self.check(release, lambda d: d["pages"][15]["legacy_also"][0].update(legacy_image_index=20),
                   "a legacy index has more than one row")
        self.check(release, lambda d: d["pages"][30].update(legacy_also=d["pages"][15]["legacy_also"]),
                   "without a row of its own")
        self.check(release, lambda d: d["legacy_unmapped"].append({"legacy_image_index": 16, "nearest_mapped": None}),
                   "both mapped and unmapped")
        self.check(release, lambda d: d["pages"][15].pop("legacy_also"), "legacy.mapped_pages differs")


if __name__ == "__main__":
    unittest.main()
