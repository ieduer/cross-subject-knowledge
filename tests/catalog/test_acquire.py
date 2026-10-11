"""Offline tests for scripts/catalog/acquire.py against a local stub HTTP server."""
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "catalog"))
import acquire  # noqa: E402


def pdf_bytes(tag: str, size: int = 200_000) -> bytes:
    body = (tag.encode() * (size // len(tag) + 1))[:size]
    return b"%PDF-1.7\n" + body


class Stub(BaseHTTPRequestHandler):
    files: dict = {}
    cut_once: set = set()      # ids whose first response stops half-way
    ignore_range: set = set()
    requests: list = []

    def log_message(self, *a):  # silence
        pass

    def do_GET(self):
        sid = self.path.rsplit("/", 1)[-1]
        rng = self.headers.get("Range")
        Stub.requests.append((sid, rng))
        data = Stub.files.get(sid)
        if data is None:
            self.send_response(403)
            self.end_headers()
            return
        start = 0
        if rng and sid not in Stub.ignore_range:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(data):
                self.send_response(416)
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            self.send_response(200)
        payload = data[start:]
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("ETag", '"stub"')
        self.send_header("X-Xue-Source-Sha256", hashlib.sha256(data).hexdigest())
        self.end_headers()
        if sid in Stub.cut_once:
            Stub.cut_once.discard(sid)
            self.wfile.write(payload[: len(payload) // 2])
            self.wfile.flush()
            self.connection.close()
            return
        self.wfile.write(payload)


class AcquireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/pdf/"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="acq-test-"))
        Stub.files, Stub.cut_once, Stub.ignore_range, Stub.requests = {}, set(), set(), []

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def job(self, items):
        (self.tmp / "job.json").write_text(json.dumps({"items": items}))

    def item(self, sid, data, md5=None, size=None):
        return {"source_id": sid, "ti_md5": md5 or hashlib.md5(data).hexdigest(),
                "ti_size": len(data) if size is None else size}

    def run_job(self, *extra, count=10, nbytes=10**9):
        return acquire.main(["--job", str(self.tmp / "job.json"), "--state", str(self.tmp / "state.json"),
                             "--store", str(self.tmp / "pdf"), "--base-url", self.base, "--pause", "0",
                             "--max-count", str(count), "--max-bytes", str(nbytes), "--max-seconds", "60",
                             "--timeout", "20", "--socket-timeout", "5", *extra])

    def state(self):
        return json.loads((self.tmp / "state.json").read_text())["items"]

    def stored(self):
        return sorted(p.name for p in (self.tmp / "pdf").glob("*.pdf"))

    def test_verified_and_stored_by_sha256(self):
        data = pdf_bytes("a")
        Stub.files["id-a"] = data
        self.job([self.item("id-a", data)])
        self.assertEqual(self.run_job(), acquire.EXIT_OK)
        st = self.state()["id-a"]
        self.assertEqual(st["state"], "verified")
        self.assertEqual(st["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(st["md5_check"], "equal")
        self.assertEqual(self.stored(), [st["sha256"] + ".pdf"])
        self.assertTrue(st["retrieved_at"] and st["retrieved_via"].endswith("/pdf/id-a"))
        self.assertEqual(st["http"]["headers"]["etag"], '"stub"')

    def test_interrupted_download_resumes_with_range(self):
        data = pdf_bytes("b", 400_000)
        Stub.files["id-b"] = data
        Stub.cut_once.add("id-b")
        self.job([self.item("id-b", data)])
        self.assertEqual(self.run_job(), acquire.EXIT_OK)
        self.assertEqual(len(Stub.requests), 2)
        self.assertIsNone(Stub.requests[0][1])
        self.assertTrue(Stub.requests[1][1].startswith("bytes="))
        self.assertGreater(int(Stub.requests[1][1].split("=")[1].rstrip("-")), 0)
        st = self.state()["id-b"]
        self.assertEqual((st["state"], st["sha256"]), ("verified", hashlib.sha256(data).hexdigest()))
        self.assertGreater(st["http"]["resumed_from"], 0)

    def test_partial_from_an_earlier_run_is_resumed(self):
        data = pdf_bytes("c", 300_000)
        Stub.files["id-c"] = data
        part = self.tmp / "pdf" / ".partial" / "id-c.part"
        part.parent.mkdir(parents=True)
        part.write_bytes(data[:100_000])
        self.job([self.item("id-c", data)])
        self.assertEqual(self.run_job(), acquire.EXIT_OK)
        self.assertEqual(Stub.requests, [("id-c", "bytes=100000-")])
        self.assertEqual(self.state()["id-c"]["state"], "verified")

    def test_md5_mismatch_fails_and_is_not_stored(self):
        data = pdf_bytes("d")
        Stub.files["id-d"] = data
        self.job([self.item("id-d", data, md5="0" * 32)])
        self.assertEqual(self.run_job(), acquire.EXIT_FAILED)
        self.assertEqual(self.state()["id-d"]["state"], "failed:md5-mismatch")
        self.assertEqual(self.stored(), [])
        self.assertFalse((self.tmp / "pdf" / ".partial" / "id-d.part").exists())
        self.assertEqual(len(Stub.requests), 2)  # exactly one automatic retry
        # a later run does not touch a failed item unless asked to
        self.assertEqual(self.run_job(), acquire.EXIT_FAILED)
        self.assertEqual(len(Stub.requests), 2)

    def test_verified_item_is_never_fetched_again(self):
        data = pdf_bytes("e")
        Stub.files["id-e"] = data
        self.job([self.item("id-e", data)])
        self.assertEqual(self.run_job(), acquire.EXIT_OK)
        self.assertEqual(self.run_job(), acquire.EXIT_OK)
        self.assertEqual(len(Stub.requests), 1)

    def test_count_cap(self):
        a, b = pdf_bytes("f"), pdf_bytes("g")
        Stub.files.update({"id-f": a, "id-g": b})
        self.job([self.item("id-f", a), self.item("id-g", b)])
        self.assertEqual(self.run_job(count=1), acquire.EXIT_CAP)
        st = self.state()
        self.assertEqual((st["id-f"]["state"], st["id-g"]["state"]), ("verified", "pending"))
        self.assertEqual(len(Stub.requests), 1)
        self.assertEqual(self.run_job(count=1), acquire.EXIT_OK)  # resumable: the next run finishes

    def test_byte_cap_stops_before_the_request(self):
        data = pdf_bytes("h", 300_000)
        Stub.files["id-h"] = data
        self.job([self.item("id-h", data)])
        self.assertEqual(self.run_job(nbytes=1000), acquire.EXIT_CAP)
        self.assertEqual(Stub.requests, [])
        self.assertEqual(self.state()["id-h"]["state"], "pending")

    def test_two_consecutive_refusals_stop_the_job(self):
        data = pdf_bytes("i")
        Stub.files["id-ok"] = data
        self.job([{"source_id": "id-missing", "ti_md5": None, "ti_size": None}, self.item("id-ok", data)])
        self.assertEqual(self.run_job(), acquire.EXIT_REFUSED)
        st = self.state()
        self.assertEqual(st["id-missing"]["state"], "failed:http-403")
        self.assertEqual(st["id-ok"]["state"], "pending")
        self.assertEqual([r[0] for r in Stub.requests], ["id-missing", "id-missing"])

    def test_dry_run_makes_no_request_and_writes_nothing(self):
        data = pdf_bytes("j")
        Stub.files["id-j"] = data
        self.job([self.item("id-j", data)])
        self.assertEqual(self.run_job("--dry-run"), acquire.EXIT_OK)
        self.assertEqual(Stub.requests, [])
        self.assertFalse((self.tmp / "state.json").exists())
        self.assertFalse((self.tmp / "pdf").exists())

    def test_same_content_under_two_ids_is_stored_once(self):
        data = pdf_bytes("k")
        Stub.files.update({"id-k1": data, "id-k2": data})
        self.job([self.item("id-k1", data), self.item("id-k2", data)])
        self.assertEqual(self.run_job(), acquire.EXIT_OK)
        self.assertEqual(len(self.stored()), 1)
        self.assertTrue(self.state()["id-k2"]["deduplicated"])

    def test_multipart_md5_is_size_only_and_recorded(self):
        data = pdf_bytes("l")
        Stub.files["id-l"] = data
        self.job([self.item("id-l", data, md5="f8e3ebf065311378561be9f6c552fd50-3")])
        self.assertEqual(self.run_job(), acquire.EXIT_OK)
        self.assertEqual(self.state()["id-l"]["md5_check"], "multipart-etag:size-only")

    def test_non_pdf_body_is_rejected(self):
        Stub.files["id-m"] = b"<html>blocked</html>" * 100
        self.job([{"source_id": "id-m", "ti_md5": None, "ti_size": None}])
        self.assertEqual(self.run_job(), acquire.EXIT_FAILED)
        self.assertEqual(self.state()["id-m"]["state"], "failed:not-pdf")
        self.assertEqual(self.stored(), [])

    def test_item_url_sha256_and_bound_header(self):
        data = pdf_bytes("x")
        Stub.files["book.pdf"] = data
        sha = hashlib.sha256(data).hexdigest()
        url = self.base.replace("/pdf/", "/books/0123456789abcdef/book.pdf")
        good = {"source_id": "xue:0123456789abcdef", "url": url, "ti_md5": None, "ti_size": len(data),
                "sha256": sha, "expect_headers": {"x-xue-source-sha256": sha}}
        self.job([good])
        self.assertEqual(self.run_job(), acquire.EXIT_OK)
        st = self.state()["xue:0123456789abcdef"]
        self.assertEqual((st["state"], st["sha256_check"], st["retrieved_via"]), ("verified", "equal", url))
        self.assertEqual(st["http"]["headers"]["x-xue-source-sha256"], sha)
        # the server announces other bytes than the binding: nothing is kept
        self.job([dict(good, source_id="xue:b", expect_headers={"x-xue-source-sha256": "0" * 64})])
        self.assertEqual(self.run_job(), acquire.EXIT_FAILED)
        self.assertEqual(self.state()["xue:b"]["state"], "failed:header-mismatch:x-xue-source-sha256")
        self.assertFalse((self.tmp / "pdf" / ".partial" / "xue:b.part").exists())
        # the body is not the bound file
        self.job([dict(good, source_id="xue:c", sha256="1" * 64, expect_headers=None)])
        self.assertEqual(self.run_job(), acquire.EXIT_FAILED)
        self.assertEqual(self.state()["xue:c"]["state"], "failed:sha256-mismatch")
        self.assertEqual(len(self.stored()), 1)

    def test_bad_job_file_is_a_usage_error(self):
        (self.tmp / "job.json").write_text(json.dumps({"items": [{"source_id": "../x"}]}))
        self.assertEqual(self.run_job(), acquire.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
