"""Offline tests: deterministic release build and the release validator's invariants."""
import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "catalog"))
import build_catalog  # noqa: E402
import page_map  # noqa: E402
import validate_release  # noqa: E402
from common import dumps, jc_short_key, write_json  # noqa: E402

SID_A = "11111111-1111-4111-8111-111111111111"
SID_B = "22222222-2222-4222-8222-222222222222"
SID_C = "33333333-3333-4333-8333-333333333333"
SHA_A, SHA_B = "a" * 64, "b" * 64
KEY_A, KEY_B, KEY_C = "初中_语文_甲", "高中_物理_乙", "高中_数学_丙"


def page_recs(n, front):
    out = []
    for p in range(1, n + 1):
        text = (f"第{p}页独有的内容是关于编号{p * 7919}的故事，主人公名叫角色{p * 104729}，"
                f"他在城市{p * 1299709}生活。") * 3
        margin = {"top": [], "bottom": [f"{p - front} 单元"] if p > front else []}
        out.append({"pdf_page": p, "chars": len(text), "kind": "text", "text": text, "margin": margin})
    return out


def edition(eid, sid, sha, key, *, basis, sid_basis, stage, subject, title, status="active", current=True, pages=40):
    return {
        "edition_id": eid, "work_id": "cnw-" + hashlib.sha256(title.encode()).hexdigest()[:16],
        "region": "CN", "stage": stage, "subject": subject, "title": title, "volume": None,
        "publisher": None, "edition_label": None, "provenance": "test",
        "source": {"provider": "smartedu", "source_id": sid, "source_id_basis": sid_basis,
                   "catalog_module": "tch_material" if sid else None, "online_time": None, "update_time": None,
                   "ti_md5": None, "ti_size": None, "platform_current": current,
                   "details_retrieved_at": "2026-10-10T10:00:00Z" if sid else None,
                   "retrieved_at": "2026-10-10T11:00:00Z" if sha else None, "retrieved_via": "test" if sha else None},
        "pdf": {"sha256": sha, "md5": "0" * 32, "bytes": 1000, "pages": pages, "has_text_layer": True,
                "producer": None, "text_pages": pages, "ocr_needed_pages": 0, "blank_pages": 0} if sha else None,
        "storage": {"vps": None, "drive": None, "upstream": {"available": bool(sid), "url": None}},
        "legacy": {"sun_book_key": key, "jc_short_key": jc_short_key(key) if key else None, "jks_slug": None,
                   "basis": basis, "in_sun_search": True, "alias_candidate": None},
        "status": status,
    }


class Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.pm, self.dl, self.det = root / "pm", root / "dl", root / "det"
        for d in (self.pm, self.dl, self.det):
            d.mkdir(parents=True)
        a = edition(f"cn-{SID_A}-{SHA_A[:12]}", SID_A, SHA_A, KEY_A, basis="bytes:source-pdf",
                    sid_basis="bytes:sha256=learn-books", stage="初中", subject="语文", title="语文甲")
        a["legacy_spec"] = {"mode": "same-bytes", "legacy_pages": 40, "sun_book_key": KEY_A}
        b = edition(f"cn-{SID_B}-{SHA_B[:12]}", SID_B, SHA_B, KEY_B, basis="identifier:jc-content-id",
                    sid_basis="identifier:jc-content-id", stage="高中", subject="物理", title="物理乙")
        b["legacy_spec"] = {"mode": "anchors", "legacy_pages": 39, "sun_book_key": KEY_B}
        c = edition(f"cn-x-legacy-{jc_short_key(KEY_C)}", None, None, KEY_C, basis="identifier:jc-book-key",
                    sid_basis=None, stage="高中", subject="数学", title="数学丙", status="pending-verification",
                    current=None)
        c["legacy_spec"] = {"mode": "none", "relation": "none", "reason": "no PDF"}
        d = edition(f"cn-{SID_C}-pending", SID_C, None, None, basis=None, sid_basis="identifier:jc-content-id",
                    stage="高中", subject="数学", title="数学丁", status="pending-verification")
        d["legacy_spec"] = {"mode": "none", "relation": "none", "reason": "not acquired"}
        self.plan = {"editions": [a, b, c, d], "newer_platform": [], "indeterminate_platform": [], "issues": []}
        recs = page_recs(40, 2)
        hits = [{"legacy_index": k, "text": recs[k + 1]["text"][:120]} for k in (2, 6, 10, 14, 18, 22, 26, 30, 34)]
        write_json(self.pm / f"{a['edition_id']}.json", page_map.build_page_map(
            a["edition_id"], SHA_A, {"extractor_version": "t", "page_texts": recs}, a["legacy_spec"]))
        write_json(self.pm / f"{b['edition_id']}.json", page_map.build_page_map(
            b["edition_id"], SHA_B, {"extractor_version": "t", "page_texts": recs}, {**b["legacy_spec"], "hits": hits}))
        rows = [{"id": SID_A, "title": "语文甲", "edition": "统编版", "publisher": "人教", "cover_url": "https://x/c"},
                {"id": "44444444-4444-4444-8444-444444444444", "title": "语文戊", "edition": "", "publisher": "",
                 "cover_url": ""}]
        write_json(self.dl / "初中__语文.json", {"retrieved_at": "2026-10-10T09:00:00Z", "body": rows,
                                              "headers": {"x-jks-catalog-version": "1"}})
        write_json(self.dl / "小学__物理.json", {"retrieved_at": "2026-10-10T09:00:01Z", "body": [], "headers": {}})
        write_json(self.det / f"{SID_A}.json", {"body": {"ti_items": [
            {"ti_format": "pdf", "ti_file_flag": "source", "ti_size": "1000"}]}})
        write_json(root / "plan.json", self.plan)

    def build(self, out: Path) -> Path:
        rc = build_catalog.main(["--plan", str(self.root / "plan.json"), "--page-maps", str(self.pm),
                                 "--downloads", str(self.dl), "--details", str(self.det), "--out", str(out)])
        assert rc == 0
        (release,) = [p for p in out.iterdir() if p.is_dir() and not p.name.startswith(".")]
        return release


def tree(path: Path) -> dict:
    return {str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


def resum(release: Path) -> None:
    """Rewrite SHA256SUMS after a deliberate mutation so that only the targeted invariant fails."""
    lines = []
    for p in sorted(release.rglob("*")):
        if p.is_file() and p.name != "SHA256SUMS":
            lines.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(release)}\n")
    (release / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")


class BuildValidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="build-test-"))
        self.fx = Fixture(self.tmp / "in")
        self.release = self.fx.build(self.tmp / "out1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- build ------------------------------------------------------------------------------
    def test_release_is_valid(self):
        self.assertEqual(validate_release.validate(self.release, {SHA_A: {}, SHA_B: {}}), [])

    def test_two_builds_are_byte_identical(self):
        again = self.fx.build(self.tmp / "out2")
        self.assertEqual(again.name, self.release.name)
        self.assertEqual(tree(again), tree(self.release))
        # re-running into the same directory is a no-op
        self.assertEqual(self.fx.build(self.tmp / "out1"), self.release)

    def test_release_id_follows_the_inputs(self):
        self.assertRegex(self.release.name, r"^20261010T110000Z-[0-9a-f]{8}$")
        self.fx.plan["editions"][0]["title"] = "语文甲（改）"
        write_json(self.fx.root / "plan.json", self.fx.plan)
        other = self.fx.build(self.tmp / "out3")
        self.assertNotEqual(other.name, self.release.name)

    def test_catalog_content(self):
        cat = json.loads((self.release / "catalog.json").read_text())
        by = {e["edition_id"]: e for e in cat["editions"]}
        a, b = by[f"cn-{SID_A}-{SHA_A[:12]}"], by[f"cn-{SID_B}-{SHA_B[:12]}"]
        self.assertEqual((a["corpus"]["relation"], a["corpus"]["coverage"]), ("same-bytes", 1.0))
        self.assertEqual((b["corpus"]["relation"], b["legacy"]["offset"]), ("aligned", 1))
        self.assertNotIn("legacy_spec", a)
        self.assertTrue(a["printed"]["verified"])
        self.assertEqual(by[f"cn-{SID_C}-pending"]["corpus"]["relation"], "none")
        self.assertEqual(len(cat["reader_books"]), 3)
        dl = json.loads((self.release / "downloads.json").read_text())
        rows = {r["source_id"]: r for r in dl["rows"]}
        self.assertEqual(rows[SID_A]["edition_id"], a["edition_id"])
        self.assertEqual(rows[SID_A]["size_bytes"], 1000)
        other = rows["44444444-4444-4444-8444-444444444444"]
        self.assertEqual((other["edition_id"], other["size_bytes"], other["edition_label"], other["publisher"]),
                         (None, None, None, None))        # unknown stays null
        self.assertEqual([g["rows"] for g in dl["groups"]], [0, 2])   # empty groups are listed, not dropped
        rel = json.loads((self.release / "RELEASE.json").read_text())
        self.assertEqual(rel["counts"]["editions_with_pdf"], 2)
        self.assertEqual(rel["counts"]["by_corpus_relation"], {"same-bytes": 1, "aligned": 1, "none": 2})

    # ---- validator ----------------------------------------------------------------------------
    def mutate(self, fn, name="catalog.json"):
        path = self.release / name
        doc = json.loads(path.read_text())
        fn(doc)
        path.write_text(dumps(doc), encoding="utf-8")
        resum(self.release)
        return validate_release.validate(self.release, {SHA_A: {}, SHA_B: {}})

    def assertViolation(self, errs, needle):
        self.assertTrue(any(needle in e for e in errs), f"{needle!r} not in {errs}")

    def test_sha256sums_mismatch(self):
        p = self.release / "downloads.json"
        p.write_text(p.read_text() + " ", encoding="utf-8")
        self.assertViolation(validate_release.validate(self.release), "hash mismatch: downloads.json")

    def test_unlisted_file(self):
        (self.release / "extra.json").write_text("{}")
        self.assertViolation(validate_release.validate(self.release), "file not listed: extra.json")

    def test_orphan_page_map(self):
        shutil.copy(self.release / "page-maps" / f"cn-{SID_A}-{SHA_A[:12]}.json",
                    self.release / "page-maps" / "cn-x-ffffffffffffffff.json")
        resum(self.release)
        self.assertViolation(validate_release.validate(self.release), "no such edition in the catalog")

    def test_missing_page_map(self):
        (self.release / "page-maps" / f"cn-{SID_B}-{SHA_B[:12]}.json").unlink()
        resum(self.release)
        self.assertViolation(validate_release.validate(self.release), "has no page map")

    def test_pdf_referenced_twice(self):
        def fn(doc):
            twin = copy.deepcopy(doc["editions"][0])
            twin["edition_id"] += "x"
            twin["legacy"]["sun_book_key"] = None
            doc["editions"].append(twin)
        errs = self.mutate(fn)
        self.assertViolation(errs, "referenced by 2 editions")

    def test_stored_pdf_not_referenced_and_referenced_pdf_not_stored(self):
        errs = validate_release.validate(self.release, {SHA_A: {}, "c" * 64: {}})
        self.assertViolation(errs, f"stored PDF {'c' * 16} is referenced by 0 editions")
        self.assertViolation(errs, f"referenced PDF {SHA_B[:16]} is not in the store")

    def test_two_active_editions_of_one_work(self):
        def fn(doc):
            eds = [e for e in doc["editions"] if e["pdf"]]
            eds[1]["work_id"] = eds[0]["work_id"]
        errs = self.mutate(fn)
        self.assertViolation(errs, "active editions without an explicit coexists_reason")

        def mark(doc):
            for e in doc["editions"]:
                if e["pdf"]:
                    e["coexists_reason"] = "two publishers' printings kept on purpose"
        self.assertEqual(self.mutate(mark), [])

    def test_edition_merged_by_title_is_caught(self):
        def fn(doc):
            doc["editions"][0]["legacy"]["basis"] = "title-match"
        self.assertViolation(self.mutate(fn), "must not be merged by title")

    def test_source_id_attached_by_title_is_caught(self):
        def fn(doc):
            doc["editions"][0]["source"]["source_id_basis"] = "title-match"
        self.assertViolation(self.mutate(fn), "must not be merged by title")

    def test_same_reader_book_claimed_twice(self):
        def fn(doc):
            eds = [e for e in doc["editions"] if e["pdf"]]
            eds[1]["legacy"]["sun_book_key"] = eds[0]["legacy"]["sun_book_key"]
            eds[1]["legacy"]["jc_short_key"] = eds[0]["legacy"]["jc_short_key"]
        self.assertViolation(self.mutate(fn), "sun_book_key claimed by 2 editions")

    def test_edition_id_must_follow_the_content_hash(self):
        def fn(doc):
            doc["editions"][0]["pdf"]["sha256"] = "d" * 64
        errs = self.mutate(fn)
        self.assertViolation(errs, "edition_id does not follow the identity rule")

    def test_printed_label_on_unverified_book(self):
        def fn(pm):
            pm["printed"]["verified"] = False
        errs = self.mutate(fn, f"page-maps/cn-{SID_A}-{SHA_A[:12]}.json")
        self.assertViolation(errs, "failed folio verification")

    def test_legacy_index_without_proven_relation(self):
        def fn(doc):
            for e in doc["editions"]:
                if e["edition_id"].startswith(f"cn-{SID_B}"):
                    e["corpus"]["relation"] = "different-edition"
        self.assertViolation(self.mutate(fn), "legacy_image_index filled while corpus.relation is 'different-edition'")

    def test_legacy_index_without_accepted_mapping(self):
        def fn(pm):
            pm["legacy"]["evidence"]["accepted"] = False
        errs = self.mutate(fn, f"page-maps/cn-{SID_B}-{SHA_B[:12]}.json")
        self.assertViolation(errs, "(mapping accepted: False)")

    def test_page_rows_must_cover_the_pdf(self):
        def fn(pm):
            pm["pages"].pop()
        errs = self.mutate(fn, f"page-maps/cn-{SID_B}-{SHA_B[:12]}.json")
        self.assertViolation(errs, "rows are not exactly pdf_page 1..40")

    def test_download_row_pointing_at_missing_edition(self):
        def fn(doc):
            doc["rows"][0]["edition_id"] = "cn-x-0000000000000000"
            doc["rows"].append(dict(doc["rows"][0]))
        errs = self.mutate(fn, "downloads.json")
        self.assertViolation(errs, "is not a held edition")
        self.assertViolation(errs, "listed 2 times")

    def test_active_edition_without_pdf(self):
        def fn(doc):
            for e in doc["editions"]:
                if e["edition_id"].endswith("-pending"):
                    e["status"] = "active"
        self.assertViolation(self.mutate(fn), "active edition without a PDF")


if __name__ == "__main__":
    unittest.main()
