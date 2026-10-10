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
SHA_A, SHA_B, SHA_E = "a" * 64, "b" * 64, "e" * 64
KEY_A, KEY_B, KEY_C, KEY_E = "初中_语文_甲", "高中_物理_乙", "高中_数学_丙", "高中_数学_丁"
HOLD = {SHA_A: {}, SHA_B: {}, SHA_E: {}}


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
        "metadata_basis": "smartedu-details" if (sid and current) else "local-download-index",
        "platform_current": bool(current), "newer_edition_id": None, "supersedes_edition_id": None,
        "source": {"provider": "smartedu", "source_id": sid, "source_id_basis": sid_basis,
                   "id_basis": None if not sid else ("md5-size-current" if sha else "platform-record"),
                   "catalog_module": "tch_material" if sid else None, "online_time": None, "update_time": None,
                   "ti_md5": None, "ti_size": None, "platform_current": current,
                   "details_retrieved_at": "2026-10-10T10:00:00Z" if sid else None,
                   "retrieved_at": "2026-10-10T11:00:00Z" if sha else None, "retrieved_via": "test" if sha else None},
        "pdf": {"sha256": sha, "md5": "0" * 32, "bytes": 1000, "pages": pages, "has_text_layer": True,
                "producer": None, "text_pages": pages, "ocr_needed_pages": 0, "blank_pages": 0} if sha else None,
        "storage": {"vps": None, "drive": None,
                    "upstream": {"available": bool(sid and current),
                                 "url": f"https://jks-ai.bdfz.net/pdf/{sid}" if (sid and current) else None,
                                 "note": None}},
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
        # an older held file of SID_C: the platform now serves another file (the pending record d)
        e = edition(f"cn-{SID_C}-{SHA_E[:12]}", SID_C, SHA_E, KEY_E, basis="bytes:source-pdf",
                    sid_basis="bytes:sha256=learn-books", stage="高中", subject="数学", title="数学丁（旧档）",
                    current=False)
        e["source"]["id_basis"] = "baseline-join-unreproven"
        e["work_id"] = d["work_id"]
        e["newer_edition_id"], d["supersedes_edition_id"] = d["edition_id"], e["edition_id"]
        e["legacy_spec"] = {"mode": "same-bytes", "legacy_pages": 40, "sun_book_key": KEY_E}
        self.plan = {"editions": [a, b, c, d, e], "newer_platform": [{"source_id": SID_C}],
                     "indeterminate_platform": [], "issues": []}
        recs = page_recs(40, 2)
        write_json(self.pm / f"{e['edition_id']}.json", page_map.build_page_map(
            e["edition_id"], SHA_E, {"extractor_version": "t", "page_texts": recs}, e["legacy_spec"]))
        hits = [{"legacy_index": k, "text": recs[k + 1]["text"][:120]} for k in (2, 6, 10, 14, 18, 22, 26, 30, 34)]
        write_json(self.pm / f"{a['edition_id']}.json", page_map.build_page_map(
            a["edition_id"], SHA_A, {"extractor_version": "t", "page_texts": recs}, a["legacy_spec"]))
        write_json(self.pm / f"{b['edition_id']}.json", page_map.build_page_map(
            b["edition_id"], SHA_B, {"extractor_version": "t", "page_texts": recs}, {**b["legacy_spec"], "hits": hits}))
        rows = [{"id": SID_A, "title": "语文甲", "edition": "统编版", "publisher": "人教",
                 "cover_url": f"https://jks-ai.bdfz.net/cover/{SID_A}"},
                {"id": SID_C, "title": "数学丁", "edition": "", "publisher": "", "cover_url": ""},
                {"id": "44444444-4444-4444-8444-444444444444", "title": "语文戊", "edition": "", "publisher": "",
                 "cover_url": ""}]
        write_json(self.dl / "初中__语文.json", {"retrieved_at": "2026-10-10T09:00:00Z", "body": rows,
                                              "headers": {"x-jks-catalog-version": "1"}})
        write_json(self.dl / "小学__物理.json", {"retrieved_at": "2026-10-10T09:00:01Z", "body": [], "headers": {}})
        write_json(self.det / f"{SID_A}.json", {"body": {"ti_items": [
            {"ti_format": "pdf", "ti_file_flag": "source", "ti_size": "1000"}]}})
        write_json(root / "plan.json", self.plan)

    def build(self, out: Path, *extra) -> Path:
        rc = build_catalog.main(["--plan", str(self.root / "plan.json"), "--page-maps", str(self.pm),
                                 "--downloads", str(self.dl), "--details", str(self.det), "--out", str(out),
                                 *extra])
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
        self.assertEqual(validate_release.validate(self.release, HOLD), [])

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
        self.assertEqual(len(cat["reader_books"]), 4)
        self.assertEqual((a["corpus"]["text_identity"], b["corpus"]["text_identity"]),
                         ("same-file", "page-aligned-other-printing"))
        self.assertIsNone(by[f"cn-{SID_C}-pending"]["corpus"]["text_identity"])
        dl = json.loads((self.release / "downloads.json").read_text())
        rows = {r["source_id"]: r for r in dl["rows"]}
        self.assertEqual(rows[SID_A]["edition_id"], a["edition_id"])
        self.assertEqual(rows[SID_A]["size_bytes"], 1000)
        other = rows["44444444-4444-4444-8444-444444444444"]
        self.assertEqual((other["edition_id"], other["size_bytes"], other["edition_label"], other["publisher"]),
                         (None, None, None, None))        # unknown stays null
        self.assertEqual([g["rows"] for g in dl["groups"]], [0, 3])   # empty groups are listed, not dropped
        # the upstream source of a replaced work belongs to the NEWER (pending) record, not to the held file
        old = rows[SID_C]
        self.assertEqual((old["edition_id"], old["held_other_edition_ids"]), (None, [f"cn-{SID_C}-{SHA_E[:12]}"]))
        self.assertEqual(old["sources"], [{"kind": "upstream", "url": f"https://jks-ai.bdfz.net/pdf/{SID_C}",
                                           "edition_id": f"cn-{SID_C}-pending", "platform_current": True}])
        e = by[f"cn-{SID_C}-{SHA_E[:12]}"]
        self.assertEqual((e["platform_current"], e["newer_edition_id"]), (False, f"cn-{SID_C}-pending"))
        self.assertEqual(by[f"cn-{SID_C}-pending"]["supersedes_edition_id"], e["edition_id"])
        rel = json.loads((self.release / "RELEASE.json").read_text())
        self.assertEqual(rel["counts"]["editions_with_pdf"], 3)
        self.assertEqual(rel["counts"]["by_corpus_relation"], {"same-bytes": 2, "aligned": 1, "none": 2})
        self.assertEqual(rel["counts"]["platform_current_with_pdf"], {"true": 2, "false": 1})

    # ---- validator ----------------------------------------------------------------------------
    def mutate(self, fn, name="catalog.json", keep=False):
        """Apply one deliberate defect, validate, and (unless keep) put the release back."""
        path, sums = self.release / name, self.release / "SHA256SUMS"
        before = (path.read_bytes(), sums.read_bytes())
        doc = json.loads(path.read_text())
        fn(doc)
        path.write_text(dumps(doc), encoding="utf-8")
        resum(self.release)
        errs = validate_release.validate(self.release, HOLD)
        if not keep:
            path.write_bytes(before[0])
            sums.write_bytes(before[1])
        return errs

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
        errs = validate_release.validate(self.release, {SHA_A: {}, SHA_E: {}, "c" * 64: {}})
        self.assertViolation(errs, f"stored PDF {'c' * 16} is referenced by 0 editions")
        self.assertViolation(errs, f"referenced PDF {SHA_B[:16]} is not in the store")

    def test_two_active_editions_of_one_work(self):
        def fn(doc):
            eds = [e for e in doc["editions"] if e["pdf"]]
            eds[1]["work_id"] = eds[0]["work_id"]
        errs = self.mutate(fn, keep=True)
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

    # ---- invariants added after the independent review ------------------------------------------
    A_ID, B_ID, E_ID, D_ID = (f"cn-{SID_A}-{SHA_A[:12]}", f"cn-{SID_B}-{SHA_B[:12]}",
                              f"cn-{SID_C}-{SHA_E[:12]}", f"cn-{SID_C}-pending")

    def ed(self, doc, eid):
        return next(e for e in doc["editions"] if e["edition_id"] == eid)

    def test_held_file_must_not_carry_the_current_platform_records_title(self):
        def fn(doc):
            self.ed(doc, self.E_ID)["metadata_basis"] = "smartedu-details"
        self.assertViolation(self.mutate(fn), "although this edition is not the platform's current file")

    def test_platform_current_must_be_boolean_and_consistent(self):
        def fn(doc):
            self.ed(doc, self.A_ID)["platform_current"] = None
        self.assertViolation(self.mutate(fn), "platform_current must be a boolean")

        def fn2(doc):
            self.ed(doc, self.A_ID)["platform_current"] = True
            self.ed(doc, self.A_ID)["source"]["platform_current"] = False
        self.assertViolation(self.mutate(fn2), "platform_current differs from source.platform_current")

    def test_newer_and_supersedes_links_must_resolve(self):
        def fn(doc):
            self.ed(doc, self.E_ID)["newer_edition_id"] = "cn-x-0000000000000000"
        self.assertViolation(self.mutate(fn), "newer_edition_id does not resolve")

        def fn2(doc):
            self.ed(doc, self.D_ID)["supersedes_edition_id"] = self.A_ID
        self.assertViolation(self.mutate(fn2), "supersedes_edition_id does not resolve")

    def test_no_edition_may_be_called_latest(self):
        def fn(doc):
            self.ed(doc, self.A_ID)["note"] = "最新版"
        self.assertViolation(self.mutate(fn), "no edition may be called the latest")

    def test_id_basis_must_be_stated_and_honest(self):
        def fn(doc):
            self.ed(doc, self.A_ID)["source"]["id_basis"] = None
        self.assertViolation(self.mutate(fn), "does not say how the id was established")

        def fn2(doc):
            self.ed(doc, self.E_ID)["source"]["id_basis"] = "md5-size-current"
        self.assertViolation(self.mutate(fn2), "md5-size-current on a file the platform does not serve")

    def test_text_identity_must_fit_the_relation(self):
        def fn(doc):
            self.ed(doc, self.B_ID)["corpus"]["text_identity"] = "same-file"
        self.assertViolation(self.mutate(fn), "corpus.text_identity 'same-file' does not fit relation 'aligned'")

    def test_storage_vps_path_shape(self):
        def fn(doc):
            self.ed(doc, self.A_ID)["storage"]["vps"] = {"path": "/srv/other/a.pdf"}
        self.assertViolation(self.mutate(fn), "storage.vps.path must be exactly pdf/<pdf.sha256>.pdf")

        def fn2(doc):
            self.ed(doc, self.D_ID)["storage"]["vps"] = {"path": "pdf/x.pdf"}
        self.assertViolation(self.mutate(fn2), "storage.vps set on an edition without a PDF")

    def test_storage_drive_shape(self):
        def fn(doc):
            self.ed(doc, self.A_ID)["storage"]["drive"] = {"file_id": "x"}
        self.assertViolation(self.mutate(fn), "storage.drive must be null or")

        def fn2(doc):
            self.ed(doc, self.A_ID)["storage"]["drive"] = {
                "file_id": "x", "resource_key": None, "shared": False,
                "view_url": "https://evil.example/x", "download_url": "https://drive.google.com/uc?id=x"}
        self.assertViolation(self.mutate(fn2), "storage.drive URL host not allowed")

    def test_upstream_url_host_and_id(self):
        for bad in ("https://evil.example/pdf/" + SID_A, "http://jks-ai.bdfz.net/pdf/" + SID_A,
                    "https://jks-ai.bdfz.net/pdf/" + SID_B, "https://jks-ai.bdfz.net.evil.example/pdf/" + SID_A):
            def fn(doc, bad=bad):
                self.ed(doc, self.A_ID)["storage"]["upstream"]["url"] = bad
            self.assertViolation(self.mutate(fn), "storage.upstream.url must be")

    def test_upstream_available_only_for_the_current_file(self):
        def fn(doc):
            up = self.ed(doc, self.E_ID)["storage"]["upstream"]
            up["available"], up["url"] = True, f"https://jks-ai.bdfz.net/pdf/{SID_C}"
        self.assertViolation(self.mutate(fn), "upstream marked available for a file that is not")

    def test_host_allow_list(self):
        ok = validate_release.host_ok
        self.assertTrue(all(ok(u) for u in (
            "https://jks-ai.bdfz.net/cover/x", "https://basic.smartedu.cn/a", "https://img.rdfzer.com/pages/k/p0.webp",
            "https://img.bdfz.net/c.webp", "https://r1-ndr-oversea.ykt.cbern.com.cn/x.pdf")))
        self.assertFalse(any(ok(u) for u in (
            "http://jks-ai.bdfz.net/x", "https://smartedu.cn.evil.example/x", "https://user@jks-ai.bdfz.net/x",
            "https://jks-ai.bdfz.net:8443/x", "javascript:alert(1)", "//jks-ai.bdfz.net/x", None)))

    def test_reader_books_index_is_validated(self):
        def dup(doc):
            doc["reader_books"].append(dict(doc["reader_books"][0]))
        self.assertViolation(self.mutate(dup), "reader_books: key listed 2 times")

        def wrong(doc):
            doc["reader_books"][0]["edition_id"] = doc["reader_books"][1]["edition_id"]
        self.assertViolation(self.mutate(wrong), "is not the edition holding that key")

        def short(doc):
            doc["reader_books"][0]["jc_short_key"] = "000000000000"
        self.assertViolation(self.mutate(short), "reader_books: jc_short_key != md5")

        def cand(doc):
            doc["reader_books"][0]["candidate_edition_ids"] = [self.B_ID]
        self.assertViolation(self.mutate(cand), "is not an alias candidate for that book")

        def missing(doc):
            doc["reader_books"].pop()
        self.assertViolation(self.mutate(missing), "reader_books: keys differ")

    def pm_mutate(self, eid, fn):
        return self.mutate(fn, f"page-maps/{eid}.json")

    def test_printed_labels_unique_and_equal_to_the_constant(self):
        def dup(pm):
            pm["pages"][10]["printed_label"] = pm["pages"][9]["printed_label"]
        self.assertViolation(self.pm_mutate(self.A_ID, dup), "is not pdf_page - c")

        def const(doc):
            self.ed(doc, self.A_ID)["printed"]["offset"] = 9
        self.assertViolation(self.mutate(const), "printed.offset in the catalog differs")

        def flag(doc):
            self.ed(doc, self.A_ID)["printed"]["verified"] = False
        self.assertViolation(self.mutate(flag), "printed.verified in the catalog differs")

    def test_legacy_index_within_the_image_set_and_on_the_recorded_offset(self):
        def beyond(pm):
            pm["legacy"]["evidence"]["legacy_pages"] = 20
        self.assertViolation(self.pm_mutate(self.B_ID, beyond), "legacy_image_index beyond the image set")

        def step(pm):
            row = next(r for r in pm["pages"] if r["legacy_basis"] == "between-anchors")
            nxt = pm["pages"][row["pdf_page"]]
            row["legacy_image_index"], nxt["legacy_image_index"] = None, None
            pm["pages"][row["pdf_page"] - 1]["legacy_image_index"] = row["pdf_page"] + 0   # off by one
        errs = self.pm_mutate(self.B_ID, step)
        self.assertViolation(errs, "legacy rows do not all equal pdf_page - 1 - offset")

        def off(doc):
            self.ed(doc, self.B_ID)["legacy"]["offset"] = 0
        self.assertViolation(self.mutate(off), "legacy rows do not all equal pdf_page - 1 - offset")

        def count(doc):
            self.ed(doc, self.B_ID)["legacy"]["mapped_pages"] = 1
        self.assertViolation(self.mutate(count), "legacy.mapped_pages differs")

    def test_relation_must_match_the_page_map_evidence(self):
        def fn(doc):
            c = self.ed(doc, self.B_ID)["corpus"]
            c["relation"] = "partial"
        self.assertViolation(self.mutate(fn), "differs from the page-map evidence 'aligned'")

        def end(pm):
            pm["legacy"]["evidence"]["reaches_end"] = False
        self.assertViolation(self.pm_mutate(self.B_ID, end), "aligned requires an accepted mapping that reaches the end")

    def test_relation_with_rows_needs_rows(self):
        def fn(pm):
            for r in pm["pages"]:
                r["legacy_image_index"], r["legacy_basis"] = None, None
            pm["legacy"]["mapped_pages"] = 0
        errs = self.pm_mutate(self.B_ID, fn)
        self.assertViolation(errs, "without any mapped row")

    def test_between_anchor_rows_only_inside_narrow_stretches(self):
        def fn(pm):
            # demote anchors so that the remaining ones are more than 16 pages apart
            for r in pm["pages"]:
                if r["legacy_basis"] == "anchor" and 8 < r["pdf_page"] < 34:
                    r["legacy_basis"] = "between-anchors"
        self.assertViolation(self.pm_mutate(self.B_ID, fn), "filled between anchors more than 16 pages apart")

    def test_image_only_pdf_has_no_labels_and_no_anchored_rows(self):
        def fn(doc):
            self.ed(doc, self.B_ID)["pdf"]["has_text_layer"] = False
        errs = self.mutate(fn)
        self.assertViolation(errs, "printed labels on a PDF without a text layer")
        self.assertViolation(errs, "text-anchored legacy rows on a PDF without a text layer")

    def dl_row(self, doc, sid):
        return next(r for r in doc["rows"] if r["source_id"] == sid)

    def test_download_links_resolve(self):
        def other(doc):
            self.dl_row(doc, SID_C)["held_other_edition_ids"] = [self.A_ID]
        self.assertViolation(self.mutate(other, "downloads.json"), "is not an older held edition of that id")

        def current(doc):
            self.dl_row(doc, SID_C)["edition_id"] = self.E_ID
        self.assertViolation(self.mutate(current, "downloads.json"), "is not the platform's current file")

        def cover(doc):
            self.dl_row(doc, SID_A)["cover"] = "https://evil.example/c.png"
        self.assertViolation(self.mutate(cover, "downloads.json"), "cover URL host not allowed")

        def url(doc):
            self.dl_row(doc, SID_A)["sources"][0]["url"] = "https://evil.example/pdf/" + SID_A
        self.assertViolation(self.mutate(url, "downloads.json"), "upstream URL not allowed")

    def test_upstream_source_belongs_to_the_newer_edition(self):
        def fn(doc):
            self.dl_row(doc, SID_C)["sources"][0]["edition_id"] = self.E_ID
        self.assertViolation(self.mutate(fn, "downloads.json"), "upstream source is attributed to an edition that is not")

        def offline(doc):
            self.dl_row(doc, SID_C)["upstream_status"] = "OFFLINE"
        self.assertViolation(self.mutate(offline, "downloads.json"), "although the platform record is OFFLINE")

        def foreign(doc):
            self.dl_row(doc, SID_C)["sources"][0]["edition_id"] = self.A_ID
        self.assertViolation(self.mutate(foreign, "downloads.json"), "which is not an edition of that id")

    def test_release_counts_are_recomputed(self):
        for key, value in (("pdf_pages", 1), ("by_corpus_relation", {}), ("printed_verified", {"true": 99}),
                           ("ocr_needed_pages", 5), ("platform_current_with_pdf", {})):
            def fn(doc, key=key, value=value):
                doc["counts"][key] = value
            self.assertViolation(self.mutate(fn, "RELEASE.json"), f"counts.{key} differs")

    # ---- staged files: --vps-holdings at build time, --holdings <dir> at validation time ----------
    def staged_release(self):
        """Rebuild the fixture around three real PDFs so that hashes and page counts can be checked."""
        import pymupdf
        store = self.tmp / "store"
        store.mkdir()
        plan = json.loads((self.fx.root / "plan.json").read_text())
        for e in plan["editions"]:
            if not e["pdf"]:
                continue
            doc = pymupdf.open()
            for i in range(40):
                doc.new_page(width=200, height=200).insert_text((20, 40), f"{e['title']} {i}")
            tmp = store / "t.pdf"
            doc.save(str(tmp))
            doc.close()
            sha = hashlib.sha256(tmp.read_bytes()).hexdigest()
            old_id, old_sha = e["edition_id"], e["pdf"]["sha256"]
            e["pdf"]["sha256"], e["pdf"]["bytes"] = sha, tmp.stat().st_size
            e["edition_id"] = old_id.replace(old_sha[:12], sha[:12])
            tmp.rename(store / f"{sha}.pdf")
            pm = json.loads((self.fx.pm / f"{old_id}.json").read_text())
            pm["edition_id"], pm["pdf_sha256"] = e["edition_id"], sha
            write_json(self.fx.pm / f"{e['edition_id']}.json", pm)
            for o in plan["editions"]:
                for k in ("newer_edition_id", "supersedes_edition_id"):
                    if o.get(k) == old_id:
                        o[k] = e["edition_id"]
        write_json(self.fx.root / "plan.json", plan)
        return store, plan

    def test_vps_holdings_sets_storage_only_for_verified_files(self):
        store, plan = self.staged_release()
        held = [e for e in plan["editions"] if e["pdf"]]
        (store / f"{held[2]['pdf']['sha256']}.pdf").unlink()                 # one file is not staged
        release = self.fx.build(self.tmp / "out-staged", "--vps-holdings", str(store))
        cat = json.loads((release / "catalog.json").read_text())
        vps = {e["edition_id"]: e["storage"]["vps"] for e in cat["editions"]}
        self.assertEqual(vps[held[0]["edition_id"]], {"path": f"pdf/{held[0]['pdf']['sha256']}.pdf"})
        self.assertIsNone(vps[held[2]["edition_id"]])
        self.assertTrue(all(e["storage"]["drive"] is None for e in cat["editions"]))
        self.assertTrue(all(v is None for k, v in vps.items() if k.endswith("-pending") or "legacy" in k))
        errs = validate_release.validate(release, store)
        self.assertViolation(errs, "is not in the store")
        # a staged older file is offered as its own, labelled source next to the upstream one
        dl = json.loads((release / "downloads.json").read_text())
        row = next(r for r in dl["rows"] if r["source_id"] == SID_C)
        kinds = {(s["kind"], s["platform_current"]) for s in row["sources"]}
        self.assertIn(("upstream", True), kinds)

    def test_vps_holdings_rejects_a_file_that_does_not_hash_to_its_name(self):
        store, _ = self.staged_release()
        (store / ("f" * 64 + ".pdf")).write_bytes(b"%PDF-1.4 not that file")
        with self.assertRaises(SystemExit):
            self.fx.build(self.tmp / "out-bad", "--vps-holdings", str(store))

    def test_holdings_directory_is_hashed_and_page_counted(self):
        store, plan = self.staged_release()
        release = self.fx.build(self.tmp / "out-staged", "--vps-holdings", str(store))
        self.assertEqual(validate_release.validate(release, store), [])
        victim = sorted(store.glob("*.pdf"))[0]
        # same name, other content
        original = victim.read_bytes()
        victim.write_bytes(original + b"\n%tampered")
        self.assertViolation(validate_release.validate(release, store), "not to its name")
        victim.write_bytes(original)
        # the catalog claims another page count than the stored file has

        def fn(doc):
            for e in doc["editions"]:
                if e["pdf"] and e["pdf"]["sha256"] == victim.stem:
                    e["pdf"]["pages"] = 41
        path = release / "catalog.json"
        doc = json.loads(path.read_text())
        fn(doc)
        path.write_text(dumps(doc), encoding="utf-8")
        resum(release)
        self.assertViolation(validate_release.validate(release, store), "40 pages, catalog says 41")


if __name__ == "__main__":
    unittest.main()
