"""Page images made from the catalogued PDFs (backend/page_images.py, page_render_worker.py).

Run:  python -m unittest discover -s tests/library
Needs fastapi, httpx, pymupdf and pillow (the last two are what the render child uses).
Nothing here contacts a host.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import library  # noqa: E402
import page_images  # noqa: E402
import synthetic  # noqa: E402

R3_REAL_DIR = Path(os.environ.get("SUN_R3_RELEASE_DIR",
                                  "/private/tmp/cf-task-sun-consolidation-20261010/catalog-release/20261011T015939Z-4fbb031a"))
R3_REAL_PDFS = Path(os.environ.get("SUN_R3_PDF_DIR", "/private/tmp/cf-task-sun-consolidation-20261010/acquire/pdf-all-r3"))
LEGACY_URL_RE = re.compile(r"img\.rdfzer\.com/pages/[0-9a-f]{12}/")


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def webp_size(data: bytes) -> tuple[int, int]:
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "WEBP"
        return image.size


def tiny_webp(width: int) -> bytes:
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (width, int(width * 1.4)), (120, 149, 148)).save(buffer, "WEBP", quality=80)
    return buffer.getvalue()


def odd_pdf(pdf_dir: Path, pages: list[tuple[float, float]]) -> dict:
    import pymupdf
    document = pymupdf.open()
    for width, height in pages:
        document.new_page(width=width, height=height).draw_rect(pymupdf.Rect(5, 5, width - 5, height - 5), color=(0, 0, 0))
    data = document.tobytes()
    document.close()
    sha = hashlib.sha256(data).hexdigest()
    (pdf_dir / f"{sha}.pdf").write_bytes(data)
    return {"sha256": sha, "bytes": len(data), "pages": len(pages), "has_text_layer": False}


class Base(unittest.TestCase):
    """A revision-4 shaped release on top of the synthetic PDFs."""

    S1 = "aaaa0000-1111-4222-8333-444444444444"   # reader book with rows, gaps and nearest hints; has covers
    S2 = "bbbb0000-1111-4222-8333-444444444444"   # reassembled from legacy images; no pre-rendered cover
    S3 = "cccc0000-1111-4222-8333-444444444444"   # odd page sizes
    S4 = "dddd0000-1111-4222-8333-444444444444"   # learn-only
    OTHER_KEY = "高中_英语_样例_重组"
    env_extra: dict = {}

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sun-page-images-"))
        cls.data_root = cls.tmp / "data"
        cls.pdf_dir = cls.tmp / "pdf"
        cls.state_root = cls.tmp / "state"
        cls.pdf_dir.mkdir()
        base = synthetic.build(cls.tmp / "seed" / "catalog", cls.pdf_dir)
        cls.seed = base
        cls.odd = odd_pdf(cls.pdf_dir, [(420, 595), (595, 1200), (300, 2400), (595, 842)])
        pdf1, pdf2 = dict(base["e1"]["pdf"]), dict(base["e2"]["pdf"])
        cls.ids = {"main": f"cn-{cls.S1}-{pdf1['sha256'][:12]}", "re": f"cn-{cls.S2}-{pdf2['sha256'][:12]}",
                   "odd": f"cn-{cls.S3}-{cls.odd['sha256'][:12]}", "learn": f"cn-{cls.S4}-{base['e4']['pdf']['sha256'][:12]}"}

        def edition(key, sid, pdf, work, **over):
            record = {
                "edition_id": cls.ids[key], "work_id": work, "region": "CN", "stage": "高中", "subject": "语文",
                "title": "普通高中教科书·语文 必修 上册", "volume": None, "publisher": "人民教育出版社", "edition_label": "统编版",
                "holding": "sun", "preview_default": True, "platform_current": True, "newer_edition_id": None,
                "supersedes_edition_id": None, "provenance": "platform-current", "cover": None,
                "source": {"provider": "smartedu", "source_id": sid, "retrieved_at": "2026-10-11T01:16:19Z"},
                "pdf": dict(pdf), "storage": {"vps": {"path": f"pdf/{pdf['sha256']}.pdf"}, "drive": None, "learn": None,
                                              "upstream": {"available": True}},
                "legacy": {"sun_book_key": None, "jc_short_key": None, "jks_slug": None, "alias_candidate": None, "offset": None},
                "corpus": {"relation": "none", "basis": "x", "coverage": None, "text_identity": None},
                "processing": {}, "status": "active",
            }
            record.update(over)
            return record

        main = edition("main", cls.S1, pdf1, "cnw-1",
                       legacy={"sun_book_key": synthetic.REAL_SUN_KEY, "jc_short_key": library.short_key_for(synthetic.REAL_SUN_KEY),
                               "jks_slug": None, "alias_candidate": None, "offset": None},
                       corpus={"relation": "partial", "basis": "content", "coverage": 0.8, "text_identity": "page-aligned-other-printing"},
                       cover={"w240": f"covers/{cls.ids['main']}-240.webp", "w480": f"covers/{cls.ids['main']}-480.webp",
                              "source_pdf_page": 1})
        reassembled = edition("re", cls.S2, pdf2, "cnw-2", subject="英语", title="普通高中教科书·英语 选择性必修 第二册",
                              platform_current=False, preview_default=True, provenance="reassembled-from-legacy-images",
                              legacy={"sun_book_key": cls.OTHER_KEY, "jc_short_key": library.short_key_for(cls.OTHER_KEY),
                                      "jks_slug": None, "alias_candidate": None, "offset": 0},
                              corpus={"relation": "legacy-image-source", "basis": "construction", "coverage": 1.0,
                                      "text_identity": "legacy-images"},
                              # names a cover that belongs to another edition: must be ignored
                              cover={"w240": f"covers/{cls.ids['main']}-240.webp", "w480": "covers/../catalog.json"})
        odd = edition("odd", cls.S3, cls.odd, "cnw-3", subject="地理", title="页面尺寸样例")
        learn = edition("learn", cls.S4, base["e4"]["pdf"], "cnw-4", holding="learn-only", preview_default=False,
                        storage={"vps": None, "drive": None, "learn": {"asset_id": "x", "key": "books/x/book.pdf"},
                                 "upstream": {"available": False}})

        def row(n, legacy=None, printed=None, verified=False):
            return {"pdf_page": n, "printed_label": printed, "printed_verified": verified, "legacy_image_index": legacy,
                    "legacy_basis": "image+text" if legacy is not None else None, "ocr_page": None}

        # main: legacy 0,1 -> pages 1,2; legacy 2,3 have no row; legacy 4..11 -> pages 5..12 (printed verified from page 5)
        main_rows = [row(1, 0), row(2, 1), row(3), row(4)] + [row(n, n - 1, str(n - 4), True) for n in range(5, 13)]
        maps = {
            cls.ids["main"]: {"edition_id": cls.ids["main"], "pdf_sha256": pdf1["sha256"], "pages": main_rows, "legacy_unmapped": [
                {"legacy_image_index": 2, "nearest_mapped": {"legacy_image_index": 1, "pdf_page": 2, "distance": 1}},
                {"legacy_image_index": 3, "nearest_mapped": {"legacy_image_index": 4, "pdf_page": 5, "distance": 1}},
                {"legacy_image_index": 12, "nearest_mapped": {"legacy_image_index": 11, "pdf_page": 12, "distance": 1}},
                # inconsistent with the rows (wrong page): must be dropped
                {"legacy_image_index": 13, "nearest_mapped": {"legacy_image_index": 11, "pdf_page": 3, "distance": 2}},
                {"legacy_image_index": 14, "nearest_mapped": None}]},
            cls.ids["re"]: {"edition_id": cls.ids["re"], "pdf_sha256": pdf2["sha256"],
                            "pages": [row(n, n - 1) for n in range(1, pdf2["pages"] + 1)], "legacy_unmapped": []},
        }
        reader_books = [
            {"sun_book_key": synthetic.REAL_SUN_KEY, "jc_short_key": library.short_key_for(synthetic.REAL_SUN_KEY), "edition_id": cls.ids["main"]},
            {"sun_book_key": cls.OTHER_KEY, "jc_short_key": library.short_key_for(cls.OTHER_KEY), "edition_id": cls.ids["re"]},
        ]
        downloads = [{"source_id": cls.S1, "stage": "高中", "subject": "语文", "title": "普通高中教科书·语文 必修 上册",
                      "edition_label": "统编版", "publisher": "人民教育出版社", "cover": f"https://jks-ai.bdfz.net/cover/{cls.S1}",
                      "size_bytes": 1, "edition_id": cls.ids["main"], "held_other_edition_ids": [],
                      "sources": [{"kind": "upstream", "url": f"https://jks-ai.bdfz.net/pdf/{cls.S1}", "platform_current": True},
                                  {"kind": "vps", "edition_id": cls.ids["main"], "platform_current": True}]},
                     {"source_id": "eeee0000-1111-4222-8333-444444444444", "stage": "小学", "subject": "语文", "title": "未存于本站",
                      "edition_label": None, "publisher": None, "cover": "https://jks-ai.bdfz.net/cover/eeee", "size_bytes": None,
                      "edition_id": None, "held_other_edition_ids": [],
                      "sources": [{"kind": "upstream", "url": "https://jks-ai.bdfz.net/pdf/eeee"}]}]
        cls.cover_bytes = {240: tiny_webp(240), 480: tiny_webp(480)}
        binary = {f"covers/{cls.ids['main']}-{w}.webp": data for w, data in cls.cover_bytes.items()}
        cls.catalog_root = cls.data_root / "catalog"
        cls.write_release(cls.catalog_root, "r4-fixture", [main, reassembled, odd, learn], downloads, maps, reader_books, binary)
        cls.app, cls.lib = cls.make_app(cls.env_extra)
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def write_release(root, name, editions, downloads, maps, reader_books, binary):
        files = {
            "catalog.json": {"contract": "sun-catalog-v1", "release_id": name, "editions": editions, "reader_books": reader_books},
            "downloads.json": {"contract": "sun-catalog-v1/downloads", "release_id": name, "rows": downloads},
            "RELEASE.json": {"contract": "sun-catalog-v1", "release_id": name, "generated_at": "2026-10-11T12:00:00Z"},
        }
        for edition_id, payload in maps.items():
            files[f"page-maps/{edition_id}.json"] = payload
        release_dir = root / "releases" / name
        (release_dir / "page-maps").mkdir(parents=True)
        (release_dir / "covers").mkdir()
        lines = []
        for rel, payload in sorted(files.items()):
            data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")
            (release_dir / rel).write_bytes(data)
            lines.append(f"{hashlib.sha256(data).hexdigest()}  {rel}")
        for rel, data in sorted(binary.items()):
            (release_dir / rel).write_bytes(data)
            lines.append(f"{hashlib.sha256(data).hexdigest()}  {rel}")
        (release_dir / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
        synthetic.set_current(root, name)

    @classmethod
    def make_app(cls, extra=None, runner=None, cache_name="cache"):
        env = {"PDF_SERVE_MODE": "direct", "LIBRARY_PDF_ROOT": str(cls.pdf_dir), "LIBRARY_RATE_RENDER_BURST": "100000",
               "LIBRARY_RATE_IMAGE_BURST": "100000", "LIBRARY_PAGE_CACHE_ROOT": str(cls.tmp / cache_name)}
        env.update(extra or {})
        app = FastAPI()
        lib = library.install(app, data_root=cls.data_root, frontend_dir=None, env=env, clock=FakeClock(),
                              state_root=cls.state_root, render_runner=runner)
        return app, lib

    def page(self, key="main", page=1, w=None, sha12=None):
        edition_id = self.ids[key]
        url = f"/files/page/{edition_id}/{sha12 or edition_id[-12:]}/{page}.webp"
        return url + (f"?w={w}" if w is not None else "")

    @staticmethod
    def fake_runner(counter, delay=0.0, gate=None, payload=b"RIFF\x00\x00\x00\x00WEBPfake"):
        def run(pdf_path, page, width, out_path, settings):
            counter.append((page, width))
            if gate is not None:
                gate.wait(10)
            if delay:
                time.sleep(delay)
            Path(out_path).write_bytes(payload + bytes(f"{page}-{width}", "ascii"))
        return run


# ── the renderer endpoint ────────────────────────────────────────────────────

class RenderEndpointTests(Base):
    def test_real_render_headers_and_cache_file(self):
        response = self.client.get(self.page(page=2, w=240))
        self.assertEqual(response.status_code, 200, response.text[:200])
        self.assertEqual(response.headers["content-type"], "image/webp")
        self.assertEqual(response.content[:4] + response.content[8:12], b"RIFFWEBP")
        self.assertEqual(webp_size(response.content), (240, 340))
        self.assertEqual(response.headers["cache-control"], "public, max-age=31536000, immutable")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["content-length"], str(len(response.content)))
        sha = self.seed["e1"]["pdf"]["sha256"]
        self.assertEqual(response.headers["etag"], f'"{sha[:32]}-2-240-{page_images.RENDER_VERSION}"')
        self.assertNotIn("set-cookie", response.headers)
        cached = self.tmp / "cache" / page_images.RENDER_VERSION / sha / "2-240.webp"
        self.assertEqual(cached.read_bytes(), response.content)          # keyed by the PDF hash, not the edition id
        self.assertEqual([p.name for p in cached.parent.iterdir() if p.name.startswith(".")], [])   # no temp file left
        self.assertEqual(oct(cached.stat().st_mode & 0o777), "0o644")
        head = self.client.head(self.page(page=2, w=240))
        self.assertEqual((head.status_code, head.content, head.headers["content-length"]), (200, b"", str(len(response.content))))
        not_modified = self.client.get(self.page(page=2, w=240), headers={"If-None-Match": response.headers["etag"]})
        self.assertEqual(not_modified.status_code, 304)
        self.assertNotIn("immutable", not_modified.headers.get("cache-control", ""))

    def test_default_width_and_each_allowed_width(self):
        self.assertEqual(webp_size(self.client.get(self.page(page=1)).content)[0], 960)
        for width in (480, 1440):
            self.assertEqual(webp_size(self.client.get(self.page(page=1, w=width)).content)[0], width)

    def test_width_must_be_in_the_fixed_set(self):
        for width in ("100", "961", "0", "-240", "abc", "960x", "99999", "240.0", "٢٤٠", ""):
            response = self.client.get(self.page(page=1) + f"?w={width}")
            expected = 200 if width == "" else 400
            self.assertEqual(response.status_code, expected, width)
            if expected == 400:
                self.assertEqual(response.headers["cache-control"], "no-store")

    def test_allow_list(self):
        main, learn = self.ids["main"], self.ids["learn"]
        sha12 = main[-12:]
        urls = [
            f"/files/page/cn-nope-000000000000/{sha12}/1.webp",            # unknown edition
            f"/files/page/{main}/000000000000/1.webp",                     # wrong hash
            f"/files/page/{main}/{sha12.upper()}/1.webp",
            f"/files/page/{main}/{sha12}/0.webp",                          # pages start at 1
            f"/files/page/{main}/{sha12}/13.webp",                         # beyond the last page
            f"/files/page/{main}/{sha12}/01.webp", f"/files/page/{main}/{sha12}/1.png", f"/files/page/{main}/{sha12}/1",
            f"/files/page/{main}/{sha12}/-1.webp", f"/files/page/{main}/{sha12}/1e1.webp", f"/files/page/{main}/{sha12}/999999.webp",
            f"/files/page/{learn}/{learn[-12:]}/1.webp",                   # learn-only: bytes exist on disk, still refused
            f"/files/page/{main}/{sha12}/..%2F..%2F1.webp", f"/files/page/..%2F{main}/{sha12}/1.webp",
            f"/files/page/{main}/{sha12}/1.webp/extra", "/files/page/../../etc/passwd",
        ]
        self.assertTrue((self.pdf_dir / f"{self.seed['e4']['pdf']['sha256']}.pdf").is_file())
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 404, url)
            self.assertNotIn("immutable", response.headers.get("cache-control", ""))
        self.assertEqual(self.client.post(self.page()).status_code, 405)

    def test_pixel_cap_and_absurd_pages(self):
        ok = self.client.get(self.page("odd", 2, 960))                    # 595x1200 at 960 -> 960x1936
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(webp_size(ok.content)[0], 960)
        self.assertIn(webp_size(ok.content)[1], (1936, 1937))
        too_many = self.client.get(self.page("odd", 2, 1440))             # 1440x2904 = 4.18 M px > 3,168,000
        self.assertEqual((too_many.status_code, too_many.headers["cache-control"]), (422, "no-store"))
        absurd = self.client.get(self.page("odd", 3, 240))                # aspect 1:8
        self.assertEqual(absurd.status_code, 422)
        self.assertEqual(self.client.get(self.page("odd", 4, 1440)).status_code, 200)   # A4 at the largest width fits
        self.assertLessEqual(1440 * 2037, page_images.MAX_OUTPUT_PIXELS)
        sha = self.odd["sha256"]
        self.assertFalse((self.tmp / "cache" / page_images.RENDER_VERSION / sha / "2-1440.webp").exists())

    def test_missing_pdf_file(self):
        app, _ = self.make_app({"LIBRARY_PDF_ROOT": str(self.tmp / "nowhere")}, cache_name="cache-missing")
        response = TestClient(app).get(self.page(page=3, w=240))
        self.assertEqual((response.status_code, response.json()["detail"]), (404, "file missing"))


class CacheHitTests(Base):
    def test_a_hit_never_reaches_the_renderer(self):
        counter = []
        app, lib = self.make_app(runner=self.fake_runner(counter), cache_name="cache-hit")
        client = TestClient(app)
        first = client.get(self.page(page=4, w=480))
        self.assertEqual((first.status_code, len(counter)), (200, 1))

        def forbidden(*args, **kwargs):
            raise AssertionError("the renderer was called on a cache hit")

        lib.render_service.runner = forbidden
        (self.pdf_dir / "moved").mkdir(exist_ok=True)
        for _ in range(3):
            again = client.get(self.page(page=4, w=480))
            self.assertEqual((again.status_code, again.content), (200, first.content))
        self.assertEqual(lib.render_service.stats()["renders"], 1)

    def test_the_web_modules_do_not_import_the_render_libraries(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import library, page_images; "
                "bad = [m for m in ('pymupdf', 'fitz', 'PIL') if m in sys.modules]; assert not bad, bad; print('CLEAN')")
        result = subprocess.run([sys.executable, "-c", code, str(ROOT / "backend")], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        source = (ROOT / "backend" / "page_images.py").read_text(encoding="utf-8") + (ROOT / "backend" / "library.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"(?m)^\s*(import|from)\s+(pymupdf|fitz|PIL)\b")

    def test_accel_mode_redirects_hits_into_the_cache_location(self):
        counter = []
        app, _ = self.make_app({"PDF_SERVE_MODE": "accel"}, runner=self.fake_runner(counter), cache_name="cache-accel")
        response = TestClient(app).get(self.page(page=6, w=240))
        sha = self.seed["e1"]["pdf"]["sha256"]
        self.assertEqual((response.status_code, response.content), (200, b""))
        self.assertEqual(response.headers["x-accel-redirect"], f"/_protected_page/{page_images.RENDER_VERSION}/{sha}/6-240.webp")
        self.assertEqual(response.headers["content-type"], "image/webp")
        self.assertEqual(response.headers["cache-control"], "public, max-age=31536000, immutable")
        self.assertTrue((self.tmp / "cache-accel" / page_images.RENDER_VERSION / sha / "6-240.webp").is_file())

    def test_default_cache_root_per_mode(self):
        state = Path("/state")
        self.assertEqual(str(page_images.ImageSettings.from_env({}, "accel", state).cache_root), "/srv/sun-textbooks/page-cache")
        self.assertEqual(str(page_images.ImageSettings.from_env({}, "direct", state).cache_root), "/state/page-cache")
        settings = page_images.ImageSettings.from_env({"LIBRARY_PAGE_ACCEL_PREFIX": "/x y\r\nSet-Cookie: a"}, "accel", state)
        self.assertEqual((settings.accel_prefix, settings.cache_max_bytes, settings.queue), ("/_protected_page/", 2 * 1024 ** 3, 8))


class QueueTests(Base):
    def test_identical_concurrent_requests_share_one_render(self):
        counter = []
        _, lib = self.make_app(runner=self.fake_runner(counter, delay=0.3), cache_name="cache-single")
        sha = self.seed["e1"]["pdf"]["sha256"]
        pdf = self.pdf_dir / f"{sha}.pdf"
        results, errors = [], []

        def ask():
            try:
                results.append(lib.render_service.ensure(pdf, sha, 7, 960))
            except Exception as error:   # pragma: no cover
                errors.append(error)

        threads = [threading.Thread(target=ask) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual((errors, len(results), len(counter)), ([], 12, 1))
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(lib.render_service.stats()["inflight"], 0)

    def test_saturated_queue_answers_503_with_retry_after(self):
        counter, gate = [], threading.Event()
        app, lib = self.make_app({"LIBRARY_RENDER_QUEUE": "2"}, runner=self.fake_runner(counter, gate=gate), cache_name="cache-queue")
        client = TestClient(app)
        sha = self.seed["e1"]["pdf"]["sha256"]
        pdf = self.pdf_dir / f"{sha}.pdf"
        waiting = [threading.Thread(target=lambda n=n: lib.render_service.ensure(pdf, sha, n, 240)) for n in (1, 2, 3)]
        try:
            for thread in waiting:
                thread.start()
            deadline = time.time() + 5
            while lib.render_service.stats()["inflight"] < 3 and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(lib.render_service.stats()["inflight"], 3)        # 1 rendering + 2 queued
            response = client.get(self.page(page=4, w=240))                    # a fourth, different page
            self.assertEqual(response.status_code, 503)
            self.assertEqual((response.headers["retry-after"], response.headers["cache-control"]), ("3", "no-store"))
            self.assertEqual(len(counter), 1)                                  # only one render ever runs at a time
        finally:
            gate.set()
            for thread in waiting:
                thread.join(10)
        self.assertEqual(sorted(counter), [(1, 240), (2, 240), (3, 240)])
        self.assertEqual(client.get(self.page(page=4, w=240)).status_code, 200)

    def test_render_time_limit_kills_the_child_and_is_remembered(self):
        slow = self.tmp / "slow_worker.py"
        slow.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        app, lib = self.make_app({"LIBRARY_RENDER_TIMEOUT_SECONDS": "1"}, cache_name="cache-timeout")
        original = page_images.WORKER
        page_images.WORKER = slow
        try:
            started = time.time()
            response = TestClient(app).get(self.page(page=8, w=240))
            elapsed = time.time() - started
            self.assertEqual(response.status_code, 504)
            self.assertLess(elapsed, 6)
            self.assertEqual(response.headers["cache-control"], "no-store")
            started = time.time()
            again = TestClient(app).get(self.page(page=8, w=240))             # not rendered again right away
            self.assertEqual(again.status_code, 504)
            self.assertLess(time.time() - started, 0.5)
        finally:
            page_images.WORKER = original
        sha = self.seed["e1"]["pdf"]["sha256"]
        folder = self.tmp / "cache-timeout" / page_images.RENDER_VERSION / sha
        self.assertEqual([p.name for p in folder.iterdir()] if folder.is_dir() else [], [])   # nothing half-written

    def test_render_bucket_limits_misses_only(self):
        counter = []
        app, _ = self.make_app({"LIBRARY_RATE_RENDER_BURST": "2", "LIBRARY_RATE_RENDER_PER_SECOND": "0.01"},
                               runner=self.fake_runner(counter), cache_name="cache-rate")
        client = TestClient(app, client=("203.0.113.50", 4000))
        codes = [client.get(self.page(page=n, w=240)).status_code for n in (1, 2, 3)]
        self.assertEqual(codes, [200, 200, 429])
        self.assertEqual([client.get(self.page(page=1, w=240)).status_code for _ in range(5)], [200] * 5)   # hits stay free
        self.assertEqual(TestClient(app, client=("203.0.113.51", 4000)).get(self.page(page=3, w=240)).status_code, 200)


class CacheStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sun-page-cache-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, cache, sha, page, size, age):
        final = cache.path_for(sha, page, 240)
        temp = cache.temp_path(final)
        temp.write_bytes(b"x" * size)
        cache.commit(temp, final)
        stamp = time.time() - age
        os.utime(final, (stamp, stamp))
        return final

    def test_eviction_keeps_the_cache_under_the_cap_oldest_first(self):
        cache = page_images.PageCache(self.tmp, max_bytes=100_000)
        sha = "a" * 64
        other_version = self.tmp / "r0" / sha
        other_version.mkdir(parents=True)
        (other_version / "1-240.webp").write_bytes(b"y" * 20_000)       # an older render version: goes first
        stale = cache.path_for(sha, 1, 240).parent
        stale.mkdir(parents=True)
        (stale / ".1-240.webp.tmp-1-1").write_bytes(b"z" * 10)
        os.utime(stale / ".1-240.webp.tmp-1-1", (time.time() - 7200, time.time() - 7200))
        files = [self.put(cache, sha, n, 10_000, age=1000 - n) for n in range(1, 15)]    # 140 kB, page 1 is the oldest
        self.assertGreater(cache.total_bytes(), 100_000)
        removed = cache.evict_if_needed()
        self.assertGreater(removed, 0)
        self.assertLessEqual(cache.total_bytes(), 100_000)
        self.assertLessEqual(sum(p.stat().st_size for p in self.tmp.rglob("*.webp")), 90_000)
        self.assertFalse((other_version / "1-240.webp").exists())
        self.assertFalse((stale / ".1-240.webp.tmp-1-1").exists())
        survivors = [p for p in files if p.exists()]
        self.assertEqual(survivors, files[-len(survivors):])                          # the newest ones survive
        self.assertFalse(files[0].exists())
        self.assertEqual(cache.evict_if_needed(), 0)

    def test_eviction_work_is_bounded_per_pass(self):
        cache = page_images.PageCache(self.tmp, max_bytes=1000)
        cache.EVICT_BATCH = 5
        for n in range(1, 31):
            self.put(cache, "b" * 64, n, 500, age=100 - n)
        self.assertEqual(cache.evict_if_needed(), 5)
        self.assertEqual(len(list(self.tmp.rglob("*.webp"))), 25)

    def test_cache_stays_bounded_through_the_service(self):
        settings = page_images.ImageSettings.from_env(
            {"LIBRARY_PAGE_CACHE_ROOT": str(self.tmp), "LIBRARY_PAGE_CACHE_MAX_BYTES": str(16 * 1024 ** 2)}, "direct", self.tmp)
        self.assertEqual(settings.cache_max_bytes, 16 * 1024 ** 2)

        def runner(pdf_path, page, width, out_path, _settings):
            Path(out_path).write_bytes(b"w" * (1024 * 1024))

        service = page_images.RenderService(settings, runner)
        for page in range(1, 41):                                                     # 40 MiB of output into a 16 MiB cache
            service.ensure(Path("unused.pdf"), "c" * 64, page, 960)
            self.assertLessEqual(sum(p.stat().st_size for p in self.tmp.rglob("*.webp")), 17 * 1024 ** 2)
        self.assertGreater(service.stats()["evictions"], 0)
        self.assertTrue(service.cache.path_for("c" * 64, 40, 960).is_file())          # the newest is still there

    def test_writes_are_atomic_and_failures_leave_nothing(self):
        settings = page_images.ImageSettings.from_env({"LIBRARY_PAGE_CACHE_ROOT": str(self.tmp)}, "direct", self.tmp)
        seen = {}

        def slow_runner(pdf_path, page, width, out_path, _settings):
            Path(out_path).write_bytes(b"half")
            seen["final_exists_mid_write"] = service.cache.path_for("d" * 64, page, width).exists()
            seen["temp_name"] = Path(out_path).name
            Path(out_path).write_bytes(b"whole-file")

        service = page_images.RenderService(settings, slow_runner)
        final = service.ensure(Path("unused.pdf"), "d" * 64, 1, 240)
        self.assertEqual((seen["final_exists_mid_write"], final.read_bytes()), (False, b"whole-file"))
        self.assertTrue(seen["temp_name"].startswith(".1-240.webp.tmp-"))
        self.assertEqual(sorted(p.name for p in final.parent.iterdir()), ["1-240.webp"])

        def failing_runner(pdf_path, page, width, out_path, _settings):
            Path(out_path).write_bytes(b"partial")
            raise page_images.RenderFailed("render-error", 500)

        service.runner = failing_runner
        with self.assertRaises(page_images.RenderFailed):
            service.ensure(Path("unused.pdf"), "d" * 64, 2, 240)
        self.assertEqual(sorted(p.name for p in final.parent.iterdir()), ["1-240.webp"])


# ── covers, legacy links, revision-4 fields ──────────────────────────────────

class CoverTests(Base):
    def test_prebuilt_cover_is_served_from_the_release(self):
        for width in (240, 480):
            response = self.client.get(f"/files/cover/{self.ids['main']}.webp?w={width}")
            self.assertEqual((response.status_code, response.content), (200, self.cover_bytes[width]))
            self.assertEqual((response.headers["content-type"], response.headers["x-content-type-options"]), ("image/webp", "nosniff"))
            self.assertEqual(response.headers["cache-control"], "public, max-age=604800")
            self.assertEqual(self.client.get(f"/files/cover/{self.ids['main']}.webp?w={width}",
                                             headers={"If-None-Match": response.headers["etag"]}).status_code, 304)
        self.assertEqual(self.client.get(f"/files/cover/{self.ids['main']}.webp").content, self.cover_bytes[240])
        self.assertEqual(self.lib.render_service.stats()["renders"], 0)

    def test_without_a_prebuilt_cover_page_one_is_rendered(self):
        for key in ("re", "odd"):       # "re" names another edition's file and a traversal path: both ignored
            response = self.client.get(f"/files/cover/{self.ids[key]}.webp?w=480", follow_redirects=False)
            self.assertEqual(response.status_code, 302, key)
            self.assertEqual(response.headers["location"], f"/files/page/{self.ids[key]}/{self.ids[key][-12:]}/1.webp?w=480")
        followed = self.client.get(f"/files/cover/{self.ids['re']}.webp?w=240")
        self.assertEqual((followed.status_code, webp_size(followed.content)[0]), (200, 240))

    def test_cover_allow_list(self):
        for url in (f"/files/cover/{self.ids['learn']}.webp", "/files/cover/cn-nope-000000000000.webp",
                    f"/files/cover/{self.ids['main']}.png", "/files/cover/..%2Fcatalog.json.webp", f"/files/cover/{self.ids['main']}"):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        for width in ("960", "100", "x"):
            self.assertEqual(self.client.get(f"/files/cover/{self.ids['main']}.webp?w={width}").status_code, 400)

    def test_cards_and_download_rows_point_at_the_local_cover(self):
        cards = {c["edition_id"]: c for c in self.client.get("/api/library/editions").json()["editions"]}
        self.assertEqual(cards[self.ids["main"]]["cover_url"], f"/files/cover/{self.ids['main']}.webp?w=240")
        rows = {r["row_id"]: r for r in self.client.get("/api/library/downloads", params={"limit": "60"}).json()["downloads"]}
        self.assertEqual(rows[self.ids["main"]]["cover_local"], f"/files/cover/{self.ids['main']}.webp?w=240")
        self.assertIsNone(rows["eeee0000-1111-4222-8333-444444444444"]["cover_local"])     # nothing held: the remote cover stays
        self.assertTrue(rows["eeee0000-1111-4222-8333-444444444444"]["cover"].startswith("https://jks-ai.bdfz.net/"))


class LegacyPageTests(Base):
    env_extra = {"LIBRARY_CORS_ORIGINS": "https://yw.bdfz.net, https://evil.example, http://s.rdfz.net, https://s.rdfz.net/, nonsense"}

    def setUp(self):
        self.key = library.short_key_for(synthetic.REAL_SUN_KEY)
        self.main = self.ids["main"]

    def test_exact_page_redirects_to_the_renderer(self):
        response = self.client.get(f"/files/legacy-page/{self.key}/p7.webp?w=480", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], f"/files/page/{self.main}/{self.main[-12:]}/8.webp?w=480")
        self.assertEqual(response.headers["cache-control"], "public, max-age=3600")
        self.assertNotIn("x-sun-legacy-approximate", response.headers)
        default = self.client.get(f"/files/legacy-page/{self.key}/p0.webp", follow_redirects=False)
        self.assertEqual(default.headers["location"], f"/files/page/{self.main}/{self.main[-12:]}/1.webp?w=960")
        followed = self.client.get(f"/files/legacy-page/{self.key}/p0.webp?w=240")
        self.assertEqual((followed.status_code, followed.headers["content-type"]), (200, "image/webp"))
        self.assertEqual(self.client.head(f"/files/legacy-page/{self.key}/p0.webp", follow_redirects=False).status_code, 302)

    def test_unmapped_page_uses_the_release_hint_and_says_so(self):
        for index, page in ((2, 2), (3, 5), (12, 12)):
            response = self.client.get(f"/files/legacy-page/{self.key}/p{index}.webp", follow_redirects=False)
            self.assertEqual(response.status_code, 302, index)
            self.assertEqual(response.headers["location"], f"/files/page/{self.main}/{self.main[-12:]}/{page}.webp?w=960")
            self.assertEqual(response.headers["x-sun-legacy-approximate"], "nearest; distance=1")

    def test_nothing_to_show_is_404_json(self):
        cases = {f"/files/legacy-page/{self.key}/p13.webp": "page-not-mapped",     # its hint contradicts the rows: dropped
                 f"/files/legacy-page/{self.key}/p14.webp": "page-not-mapped",     # hint is null
                 f"/files/legacy-page/{self.key}/p500.webp": "page-not-mapped",
                 "/files/legacy-page/ffffffffffff/p1.webp": "unknown-book"}
        for url, reason in cases.items():
            response = self.client.get(url, follow_redirects=False)
            self.assertEqual((response.status_code, response.json()["reason"], response.headers["cache-control"]), (404, reason, "no-store"), url)
        for url in (f"/files/legacy-page/{self.key}/7.webp", f"/files/legacy-page/{self.key}/p7.png", f"/files/legacy-page/{self.key.upper()}/p7.webp",
                    "/files/legacy-page/dict_xuci/p22.webp", f"/files/legacy-page/{self.key}/p-1.webp", f"/files/legacy-page/{self.key}/p7.webp/x"):
            self.assertEqual(self.client.get(url, follow_redirects=False).status_code, 404, url)
        self.assertEqual(self.client.get(f"/files/legacy-page/{self.key}/p7.webp?w=123", follow_redirects=False).status_code, 400)

    def test_reassembled_edition_keeps_old_links_exact(self):
        key = library.short_key_for(self.OTHER_KEY)
        re_id = self.ids["re"]
        for index in (0, 3, 7):
            response = self.client.get(f"/files/legacy-page/{key}/p{index}.webp", follow_redirects=False)
            self.assertEqual(response.headers["location"], f"/files/page/{re_id}/{re_id[-12:]}/{index + 1}.webp?w=960")
            self.assertNotIn("x-sun-legacy-approximate", response.headers)

    def test_json_endpoint(self):
        exact = self.client.get("/api/library/legacy-page", params={"jc": self.key, "index": "7", "w": "480"}).json()
        self.assertEqual((exact["available"], exact["exact"], exact["edition_id"], exact["pdf_page"], exact["pdf_pages"]),
                         (True, True, self.main, 8, 12))
        self.assertEqual(exact["image_url"], f"/files/page/{self.main}/{self.main[-12:]}/8.webp?w=480")
        self.assertEqual(exact["viewer_url"], f"/?view=pdf&edition={self.main}&p=8")
        self.assertEqual((exact["text_identity"], exact["preview_default"]), ("page-aligned-other-printing", True))
        self.assertNotIn("approximate", exact)
        near = self.client.get("/api/library/legacy-page", params={"jc": self.key, "index": "3"}).json()
        self.assertEqual((near["exact"], near["pdf_page"], near["approximate"]),
                         (False, 5, {"kind": "nearest", "distance": 1, "legacy_image_index": 4}))
        gone = self.client.get("/api/library/legacy-page", params={"jc": self.key, "index": "500"}).json()
        self.assertEqual((gone["available"], gone["reason"]), (False, "page-not-mapped"))
        for params in ({}, {"jc": self.key}, {"jc": "xyz", "index": "1"}, {"jc": self.key, "index": "-1"},
                       {"jc": self.key, "index": "1", "w": "7"}, {"jc": self.key, "index": "1" * 6}):
            self.assertEqual(self.client.get("/api/library/legacy-page", params=params).status_code, 400, params)

    def test_cors_only_for_the_explicit_allow_list(self):
        self.assertEqual(self.lib.images.cors_origins, ("https://yw.bdfz.net", "https://s.rdfz.net"))
        url, params = "/api/library/legacy-page", {"jc": self.key, "index": "7"}
        allowed = self.client.get(url, params=params, headers={"Origin": "https://yw.bdfz.net"})
        self.assertEqual((allowed.headers["access-control-allow-origin"], allowed.headers["vary"]), ("https://yw.bdfz.net", "Origin"))
        for origin in ("https://evil.example", "https://xt.bdfz.net", "http://yw.bdfz.net", "null", "https://yw.bdfz.net.evil.example"):
            response = self.client.get(url, params=params, headers={"Origin": origin})
            self.assertNotIn("access-control-allow-origin", response.headers, origin)
        self.assertNotIn("access-control-allow-origin", self.client.get(url, params=params).headers)
        # no other endpoint gained a CORS header
        other = self.client.get("/api/library/release", headers={"Origin": "https://yw.bdfz.net"})
        self.assertNotIn("access-control-allow-origin", other.headers)
        default_app, default_lib = self.make_app({"LIBRARY_CORS_ORIGINS": ""}, cache_name="cache-cors")
        self.assertEqual(default_lib.images.cors_origins, ())


class RevisionFourFieldTests(Base):
    def test_reassembled_edition_is_labelled_and_mapped(self):
        detail = self.client.get(f"/api/library/editions/{self.ids['re']}").json()["edition"]
        self.assertEqual((detail["provenance"], detail["corpus"]["relation"], detail["corpus"]["text_identity"]),
                         ("reassembled-from-legacy-images", "legacy-image-source", "legacy-images"))
        self.assertTrue(detail["searchable"])
        body = self.client.get("/api/library/resolve", params={"book_key": self.OTHER_KEY, "section": "5"}).json()
        self.assertEqual((body["available"], body["pdf_page"], body["text_identity"]), (True, 6, "legacy-images"))
        cards = {c["edition_id"]: c for c in self.client.get("/api/library/editions").json()["editions"]}
        self.assertEqual(cards[self.ids["re"]]["provenance"], "reassembled-from-legacy-images")

    def test_old_link_api_reports_the_nearest_page(self):
        body = self.client.get("/api/library/legacy", params={"kind": "jc", "ref": synthetic.REAL_SUN_KEY, "page": "3"}).json()
        self.assertEqual((body["available"], body["exact"], body["pdf_page"], body["reason"], body["approximate"]["distance"]),
                         (True, False, 5, "nearest", 1))
        none = self.client.get("/api/library/legacy", params={"kind": "jc", "ref": synthetic.REAL_SUN_KEY, "page": "14"}).json()
        self.assertEqual((none["exact"], none["pdf_page"], none["reason"]), (False, 1, "page-not-mapped"))
        detail = self.client.get(f"/api/library/editions/{self.ids['main']}").json()["edition"]
        self.assertEqual(detail["page_map"]["legacy_unmapped_with_hint"], 3)

    def test_page_ref_for_the_rest_of_sun(self):
        ref = self.lib.legacy_page_ref(synthetic.REAL_SUN_KEY, 6)
        self.assertEqual((ref["edition_id"], ref["pdf_page"], ref["printed_label"], ref["pdf_pages"]), (self.ids["main"], 7, "3", 12))
        self.assertEqual(ref["image_url"], f"/files/page/{self.ids['main']}/{self.ids['main'][-12:]}/7.webp?w=960")
        self.assertTrue(ref["thumb_url"].endswith("7.webp?w=240"))
        self.assertIsNone(self.lib.legacy_page_ref(synthetic.REAL_SUN_KEY, 0)["printed_label"])      # not verified: no label
        self.assertIsNone(self.lib.legacy_page_ref(synthetic.REAL_SUN_KEY, 3))                       # no row: no exact image
        near = self.lib.legacy_page_ref(synthetic.REAL_SUN_KEY, 3, nearest=True)
        self.assertEqual((near["pdf_page"], near["approximate"]["distance"]), (5, 1))
        for bad in ((None, 1), ("不存在", 1), (synthetic.REAL_SUN_KEY, -1), (synthetic.REAL_SUN_KEY, "x"), (synthetic.REAL_SUN_KEY, None)):
            self.assertIsNone(self.lib.legacy_page_ref(*bad))


# ── SUN itself no longer emits legacy page-image URLs ────────────────────────

class SunEmitsNoLegacyImageUrlTests(Base):
    PROBE = r'''
import json, re, sys
import main
from fastapi import HTTPException
pattern = re.compile(r"img\.rdfzer\.com/pages/[0-9a-f]{12}/")
out = {"books": len(main._book_map), "urls": 0, "none": 0, "viewer": 0, "viewer_404": 0, "bad": []}
keys = list(main._book_map)
sample = keys if len(sys.argv) > 1 and sys.argv[1] == "all" else keys[:40] + [k for k in keys if k == "高中_语文_普通高中教科书_语文必修_上册"]
for key in sample:
    info = main._book_map[key]
    for index in (0, 1, 5, info["pages"] // 2, info["pages"] - 1):
        url = main._book_page_url(key, index)
        if url is None:
            out["none"] += 1
        else:
            out["urls"] += 1
            if not re.fullmatch(r"/files/page/[a-z0-9._-]+/[0-9a-f]{12}/[1-9][0-9]*\.webp\?w=960", url):
                out["bad"].append(url)
        try:
            payload = main.page_image(book_key=key, page=index, context=4, phase=None)
            out["viewer"] += 1
            text = json.dumps(payload, ensure_ascii=False)
            if pattern.search(text) or "img.rdfzer.com" in text:
                out["bad"].append(text[:200])
            for page in payload["pages"]:
                assert page["url"].startswith("/files/page/") and page["thumb_url"].endswith("?w=240"), page
                assert isinstance(page["pdf_page"], int) and page["pdf_page"] >= 1
        except HTTPException as error:
            assert error.status_code == 404, error.status_code
            out["viewer_404"] += 1
out["dict_url"] = main._dict_page_url("xuci", 10)
out["sample_ref"] = main._book_page_ref("高中_语文_普通高中教科书_语文必修_上册", 6)
print(json.dumps(out, ensure_ascii=False))
'''

    def run_probe(self, data_root: Path, pdf_root: Path, mode="sample") -> dict:
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.tmp), "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8",
               "PYTHONPATH": str(ROOT / "backend"), "PYTHONDONTWRITEBYTECODE": "1", "PROJECT_ROOT": str(ROOT),
               "DATA_ROOT": str(data_root), "STATE_ROOT": str(self.tmp / "probe-state"), "RUNTIME_DB_SYNC_MODE": "disabled",
               "RERANKER_ENABLED": "0", "RERANKER_PRELOAD": "0", "SUPPLEMENTAL_VECTOR_ENABLED": "0", "HF_HUB_OFFLINE": "1",
               "PDF_SERVE_MODE": "direct", "LIBRARY_PDF_ROOT": str(pdf_root)}
        (data_root / "index").mkdir(parents=True, exist_ok=True)
        result = subprocess.run([sys.executable, "-c", self.PROBE, mode], cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_url_builders_with_the_fixture_release(self):
        out = self.run_probe(self.data_root, self.pdf_dir)
        self.assertEqual(out["bad"], [])
        self.assertGreater(out["books"], 100)
        self.assertGreater(out["urls"], 0)          # the one reader book of the fixture
        self.assertGreater(out["none"], 100)        # every other book: no held file -> null, never a legacy URL
        ref = out["sample_ref"]
        self.assertEqual((ref["edition_id"], ref["pdf_page"], ref["printed_label"]), (self.ids["main"], 7, "3"))
        self.assertTrue(out["dict_url"].startswith("https://img.rdfzer.com/pages/dict_xuci/"))   # dictionary images are not retired

    def test_no_legacy_image_url_without_any_catalog(self):
        out = self.run_probe(self.tmp / "empty-data", self.pdf_dir)
        self.assertEqual((out["bad"], out["urls"], out["viewer"]), ([], 0, 0))

    @unittest.skipUnless((R3_REAL_DIR / "catalog.json").is_file() and R3_REAL_PDFS.is_dir(), "the real revision-3 release is not on this machine")
    def test_url_builders_with_the_real_release_for_every_reader_book(self):
        root = self.tmp / "real-data"
        (root / "catalog" / "releases").mkdir(parents=True)
        (root / "catalog" / "releases" / R3_REAL_DIR.name).symlink_to(R3_REAL_DIR, target_is_directory=True)
        (root / "catalog" / "CURRENT").write_text(R3_REAL_DIR.name + "\n", encoding="utf-8")
        out = self.run_probe(root, R3_REAL_PDFS, mode="all")
        self.assertEqual(out["bad"], [])
        self.assertEqual(out["books"], 144)
        self.assertGreater(out["urls"], 600)
        self.assertGreater(out["viewer"], 300)

    def test_sources_reference_the_legacy_set_nowhere_else(self):
        main_py = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
        hits = [line.strip() for line in main_py.splitlines() if "{IMG_CDN}/pages/" in line]
        self.assertEqual(len(hits), 1, hits)                                   # the dictionary page builder only
        self.assertIn("meta['page_prefix']", hits[0])
        self.assertNotIn("_book_key_to_short.get", main_py)
        for name in ("app.js", "library.js", "dict.js", "ai-client.js"):
            text = (ROOT / "frontend" / "assets" / name).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"IMG_CDN\}?/pages|img\.rdfzer\.com/pages", name)
        for page in ("index.html", "chuzhong.html", "dict.html", "chuzhong-dict.html"):
            self.assertIsNone(LEGACY_URL_RE.search((ROOT / "frontend" / page).read_text(encoding="utf-8")), page)


# ── memory / time probe on a real book ───────────────────────────────────────

@unittest.skipUnless(R3_REAL_PDFS.is_dir() and any(R3_REAL_PDFS.glob("*.pdf")), "the held PDFs are not on this machine")
class RealRenderProbe(unittest.TestCase):
    """Renders 30 different pages of the largest held PDF at 960 px, one after another, through
    the real child process.  Prints timings and peak memory; asserts the parent does not grow."""

    PARENT_GROWTH_LIMIT_MB = 40
    CHILD_PEAK_LIMIT_MB = 1024

    def test_thirty_pages_of_the_largest_pdf(self):
        largest = max(R3_REAL_PDFS.glob("*.pdf"), key=lambda p: p.stat().st_size)
        sha = largest.stem
        tmp = Path(tempfile.mkdtemp(prefix="sun-render-probe-"))
        try:
            import pymupdf
            with pymupdf.open(str(largest)) as document:
                page_count = document.page_count
            settings = page_images.ImageSettings.from_env({"LIBRARY_PAGE_CACHE_ROOT": str(tmp)}, "direct", tmp)
            service = page_images.RenderService(settings)
            unit = 1 if sys.platform == "darwin" else 1024          # ru_maxrss: bytes on macOS, kB on Linux
            pages = sorted({max(1, min(page_count, round(1 + i * (page_count - 1) / 29))) for i in range(30)})
            before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * unit
            times, sizes = [], []
            for page in pages:
                started = time.perf_counter()
                path = service.ensure(largest, sha, page, 960)
                times.append(time.perf_counter() - started)
                sizes.append(path.stat().st_size)
            after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * unit
            child_peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * unit
            started = time.perf_counter()
            for page in pages:
                self.assertIsNotNone(service.cache.lookup(service.cache.path_for(sha, page, 960)))
            hit_time = (time.perf_counter() - started) / len(pages)
            times.sort()
            report = {
                "pdf_bytes": largest.stat().st_size, "pdf_pages": page_count, "rendered_pages": len(pages),
                "render_seconds_median": round(times[len(times) // 2], 3), "render_seconds_max": round(times[-1], 3),
                "render_seconds_total": round(sum(times), 2), "output_bytes_median": sorted(sizes)[len(sizes) // 2],
                "parent_peak_rss_growth_mb": round((after - before) / 1048576, 1),
                "child_peak_rss_mb": round(child_peak / 1048576, 1), "cache_hit_seconds": round(hit_time, 6),
            }
            print("\nRENDER_PROBE " + json.dumps(report), file=sys.stderr)
            self.assertEqual(service.stats()["renders"], len(pages))
            self.assertLess(report["parent_peak_rss_growth_mb"], self.PARENT_GROWTH_LIMIT_MB)
            self.assertLess(report["child_peak_rss_mb"], self.CHILD_PEAK_LIMIT_MB)
            self.assertLess(report["render_seconds_max"], settings.render_timeout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
