"""HTTP-level tests of ai_guard.build_router with fake builders and a fake upstream.
main.py is never imported and no network request leaves the process."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import ai_guard  # noqa: E402
from ai_guard import AIGuardError, Built, Guard, GuardConfig  # noqa: E402

KEY = "s3ss10n-key-" + "z" * 40
ORIGIN = "https://sun.bdfz.net"
JSON_HEADERS = {"Origin": ORIGIN, "Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}
CHAT = {"query": "牛顿第二定律", "user_message": "请解释", "history": [], "phase": "高中"}
EVIDENCE = [{"id": 7, "subject": "物理", "title": "必修第一册", "book_key": "bk", "section": 87, "logical_page": 92,
             "citation": "[物理·必修第一册·p92]", "snippet": "F=ma", "text": "RAW CHUNK TEXT"}]


class Harness:
    def __init__(self, tmp, *, enabled=True, peer=("127.0.0.1", 50000), **env):
        base = {"AI_ENABLED": "1", "AI_SESSION_KEY": KEY, "AI_DAILY_REQUEST_BUDGET": "50"} if enabled else {}
        base.update({k: str(v) for k, v in env.items()})
        self.cfg = GuardConfig.from_env(base, state_root=Path(tmp))
        self.upstream_calls = 0
        self.upstream_gate = asyncio.Event()
        self.upstream_gate.set()
        self.upstream_error = None
        self.build_calls = []
        self.logs = []
        self.guard = Guard(self.cfg, upstream=self.upstream, log=self.logs.append)
        app = FastAPI()
        app.include_router(ai_guard.build_router(self.guard, {"chat": self.build("chat"), "dict": self.build("dict"), "gaokao": self.build("gaokao")}))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=peer), base_url=ORIGIN)

    async def upstream(self, prompt):
        self.upstream_calls += 1
        await self.upstream_gate.wait()
        if self.upstream_error:
            raise self.upstream_error
        return "见 [物理·必修第一册·p92] 与 [物理·必修第一册·p404]。"

    def build(self, kind):
        async def builder(fields):
            self.build_calls.append((kind, fields))
            evidence = ai_guard.sanitize_evidence(EVIDENCE)
            return Built(
                prompt=f"PROMPT {kind}",
                finish=lambda answer: {"answer": answer, "provider": "Gemini", "context": {"evidence": evidence},
                                       "citation_check": ai_guard.check_citations(answer, evidence)},
                meta={"evidence_count": 1},
            )
        return builder

    async def session(self, **headers):
        return await self.client.post("/api/ai/session", headers={**JSON_HEADERS, **headers}, content=b"{}")

    async def post(self, path, payload, **headers):
        return await self.client.post(path, headers={**JSON_HEADERS, **headers}, content=json.dumps(payload, ensure_ascii=False).encode())


class HttpGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.harnesses = []

    async def asyncTearDown(self):
        for harness in self.harnesses:
            await harness.client.aclose()
        self.tmp.cleanup()

    def harness(self, **kwargs) -> Harness:
        harness = Harness(self.tmp.name, **kwargs)
        self.harnesses.append(harness)
        return harness

    async def test_disabled_by_default(self):
        h = self.harness(enabled=False)
        for path in ("/api/ai/session", "/api/chat", "/api/dict/chat", "/api/gaokao/chat"):
            for headers in (JSON_HEADERS, {"Content-Type": "application/json"}):
                response = await h.client.post(path, headers=headers, content=json.dumps(CHAT).encode())
                self.assertEqual(response.status_code, 503, path)
                self.assertEqual(response.json()["code"], "AI_DISABLED")
                self.assertNotIn("set-cookie", response.headers)
        status = (await h.client.get("/api/ai/status")).json()
        self.assertEqual(status, {"enabled": False, "missing": ["AI_ENABLED", "AI_SESSION_KEY", "AI_DAILY_REQUEST_BUDGET"], "budget": None})
        self.assertEqual((h.upstream_calls, h.build_calls), (0, []))

    async def test_partial_configuration_stays_disabled(self):
        h = self.harness(enabled=False, AI_ENABLED="1", AI_SESSION_KEY=KEY)
        self.assertEqual((await h.session()).status_code, 503)
        self.assertEqual((await h.post("/api/chat", CHAT)).json()["code"], "AI_DISABLED")

    async def test_session_cookie_attributes(self):
        h = self.harness()
        response = await h.session()
        self.assertEqual(response.status_code, 200)
        cookie = response.headers["set-cookie"]
        lowered = cookie.lower()
        for attribute in ("httponly", "secure", "samesite=strict", "path=/api", "max-age=1800"):
            self.assertIn(attribute, lowered)
        self.assertTrue(cookie.startswith(ai_guard.SESSION_COOKIE + "=v1."))
        body = response.text
        self.assertNotIn(KEY, body)
        self.assertNotIn(cookie.split(";")[0].split("=", 1)[1], body, "token is not readable by page script")
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_missing_or_invalid_token_is_401(self):
        h = self.harness()
        response = await h.post("/api/chat", CHAT)
        self.assertEqual((response.status_code, response.json()["code"]), (401, "AI_SESSION_REQUIRED"))
        h.client.cookies.set(ai_guard.SESSION_COOKIE, "v1.9999999999.nonce.forged", domain="sun.bdfz.net", path="/api")
        self.assertEqual((await h.post("/api/dict/chat", {"headword": "之", "user_message": "m"})).status_code, 401)
        self.assertEqual((await h.post("/api/gaokao/chat", {"question_id": 3})).status_code, 401)
        self.assertEqual((h.upstream_calls, h.build_calls), (0, []))

    async def test_token_is_bound_to_the_subject(self):
        h = self.harness()
        await h.session(**{"X-Real-IP": "203.0.113.10"})
        self.assertEqual((await h.post("/api/chat", CHAT, **{"X-Real-IP": "203.0.113.10"})).status_code, 200)
        other = await h.post("/api/chat", dict(CHAT, query="另一个"), **{"X-Real-IP": "203.0.113.99"})
        self.assertEqual(other.status_code, 401)

    async def test_x_real_ip_is_ignored_from_a_direct_peer(self):
        h = self.harness(peer=("198.51.100.7", 40000), AI_RATE_BURST=2, AI_RATE_PER_MINUTE=1)
        await h.session(**{"X-Real-IP": "203.0.113.1"})
        codes = []
        for index in range(4):
            # Rotating the header must not mint fresh rate-limit subjects or invalidate the token.
            response = await h.post("/api/chat", dict(CHAT, query=f"q{index}"), **{"X-Real-IP": f"203.0.113.{index + 2}"})
            codes.append(response.status_code)
        self.assertEqual(codes, [200, 200, 429, 429])

    async def test_bad_origin_is_403_before_anything_else(self):
        h = self.harness()
        await h.session()
        cases = (
            {"Origin": "https://evil.example"},
            {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"},
            {"Origin": "https://jks.bdfz.net"},
        )
        for headers in cases:
            response = await h.post("/api/chat", CHAT, **headers)
            self.assertEqual((response.status_code, response.json()["code"]), (403, "forbidden_origin"), headers)
        no_origin = await h.client.post("/api/chat", headers={"Content-Type": "application/json"}, content=json.dumps(CHAT).encode())
        self.assertEqual(no_origin.status_code, 403)
        self.assertEqual((await h.session(Origin="https://evil.example")).status_code, 403)
        self.assertEqual((h.upstream_calls, h.build_calls), (0, []))

    async def test_happy_path_cache_and_citations(self):
        h = self.harness()
        await h.session()
        first = await h.post("/api/chat", CHAT)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers["cache-control"], "no-store")
        data = first.json()
        self.assertFalse(data["cached"])
        self.assertEqual(data["context"]["evidence"][0]["book_key"], "bk")
        self.assertNotIn("text", data["context"]["evidence"][0])
        self.assertEqual(data["citation_check"]["unverified_count"], 1)
        second = await h.post("/api/chat", dict(CHAT, query="  牛顿第二定律 "))
        self.assertTrue(second.json()["cached"])
        self.assertEqual((h.upstream_calls, len(h.build_calls)), (1, 1), "one retrieval and one upstream call for one question")
        self.assertNotIn(KEY, first.text)

    async def test_three_identical_concurrent_requests_one_upstream_call(self):
        h = self.harness()
        await h.session()
        h.upstream_gate.clear()
        tasks = [asyncio.ensure_future(h.post("/api/chat", CHAT)) for _ in range(3)]
        for _ in range(200):
            await asyncio.sleep(0.005)
            if h.upstream_calls and h.guard.inflight and next(iter(h.guard.inflight.values())).waiters == 3:
                break
        h.upstream_gate.set()
        responses = await asyncio.gather(*tasks)
        self.assertEqual([r.status_code for r in responses], [200, 200, 200])
        self.assertEqual(h.upstream_calls, 1)
        self.assertEqual(h.guard.status()["budget"]["count"], 1)

    async def test_body_caps(self):
        h = self.harness(AI_MAX_BODY_BYTES=2048)
        await h.session()

        async def chunked():
            yield json.dumps(CHAT).encode()

        missing = await h.client.post("/api/chat", headers=JSON_HEADERS, content=chunked())
        self.assertEqual((missing.status_code, missing.json()["code"]), (411, "length_required"))
        big = await h.post("/api/chat", dict(CHAT, user_message="x" * 3000))
        self.assertEqual((big.status_code, big.json()["code"]), (413, "body_too_large"))
        cjk = await h.post("/api/chat", dict(CHAT, user_message="字" * 700))  # 700 chars but 2100 bytes: bytes are what count
        self.assertEqual(cjk.status_code, 413)
        wrong_type = await h.client.post("/api/chat", headers={**JSON_HEADERS, "Content-Type": "text/plain"}, content=json.dumps(CHAT).encode())
        self.assertEqual(wrong_type.status_code, 415)
        bad_json = await h.client.post("/api/chat", headers=JSON_HEADERS, content=b"{not json")
        self.assertEqual((bad_json.status_code, bad_json.json()["code"]), (400, "bad_json"))
        self.assertEqual((h.upstream_calls, h.build_calls), (0, []))

    async def test_field_caps(self):
        h = self.harness(AI_RATE_BURST=50)
        await h.session()
        cases = (
            (dict(CHAT, query="q" * 201), 413, "input_too_long"),
            (dict(CHAT, user_message="m" * 801), 413, "input_too_long"),
            (dict(CHAT, history=[{"role": "user", "content": "x"}] * 7), 413, "history_too_long"),
            (dict(CHAT, history=[{"role": "user", "content": "x" * 1201}]), 413, "history_too_long"),
            (dict(CHAT, user_message=""), 400, "bad_request"),
            ([1, 2], 400, "bad_request"),
        )
        for payload, status, code in cases:
            response = await h.post("/api/chat", payload)
            self.assertEqual((response.status_code, response.json()["code"]), (status, code))
        self.assertEqual((await h.post("/api/gaokao/chat", {"question_id": "7"})).status_code, 400)
        self.assertEqual((await h.post("/api/dict/chat", {"headword": "字" * 41, "user_message": "m"})).status_code, 413)
        self.assertEqual((h.upstream_calls, h.build_calls), (0, []))

    async def test_streamed_body_is_cut_at_the_cap_even_if_length_lies(self):
        class LyingRequest:
            headers = {"content-type": "application/json", "content-length": "10"}

            def __init__(self):
                self.read = 0

            async def stream(self):
                for _ in range(1000):
                    self.read += 1024
                    yield b"x" * 1024

        request = LyingRequest()
        with self.assertRaises(AIGuardError) as ctx:
            await ai_guard.read_capped_json(request, 4096)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (413, "body_too_large"))
        self.assertLessEqual(request.read, 4096 + 1024)

    async def test_rate_limit_returns_429_with_retry_after(self):
        h = self.harness(AI_RATE_BURST=2, AI_RATE_PER_MINUTE=6)
        await h.session()
        codes = [(await h.post("/api/chat", dict(CHAT, query=f"q{i}"))) for i in range(3)]
        self.assertEqual([r.status_code for r in codes], [200, 200, 429])
        self.assertEqual(codes[2].json()["code"], "rate_limited")
        self.assertGreaterEqual(int(codes[2].headers["retry-after"]), 1)
        self.assertEqual(h.upstream_calls, 2)

    async def test_budget_stop(self):
        h = self.harness(AI_DAILY_REQUEST_BUDGET=2, AI_RATE_BURST=50)
        await h.session()
        responses = [await h.post("/api/chat", dict(CHAT, query=f"q{i}")) for i in range(4)]
        self.assertEqual([r.status_code for r in responses], [200, 200, 503, 503])
        self.assertEqual(responses[3].json()["code"], "AI_BUDGET_EXHAUSTED")
        self.assertIn("retry-after", responses[3].headers)
        self.assertEqual(h.upstream_calls, 2)
        status = (await h.client.get("/api/ai/status")).json()
        self.assertEqual((status["budget"]["count"], status["budget"]["limit"], status["budget"]["remaining"]), (2, 2, 0))
        self.assertRegex(status["budget"]["resets_at"], r"^\d{4}-\d\d-\d\dT00:00:00Z$")
        # The dictionary and gaokao endpoints draw on the same budget.
        self.assertEqual((await h.post("/api/dict/chat", {"headword": "之", "user_message": "m"})).json()["code"], "AI_BUDGET_EXHAUSTED")
        self.assertEqual((await h.post("/api/gaokao/chat", {"question_id": 5})).json()["code"], "AI_BUDGET_EXHAUSTED")
        self.assertEqual(h.upstream_calls, 2)

    async def test_upstream_failure_is_reported_once_without_retry(self):
        h = self.harness()
        await h.session()
        h.upstream_error = AIGuardError(504, "ai_timeout")
        response = await h.post("/api/chat", CHAT)
        self.assertEqual((response.status_code, response.json()["code"]), (504, "ai_timeout"))
        self.assertEqual(h.upstream_calls, 1)

    async def test_retired_endpoints_accept_nothing(self):
        h = self.harness()
        await h.session()
        payload = {"query": "伪造", "user_message": "伪造的日志文本", "provider": "x", "success": True, "error": "注入"}
        for path in ("/api/chat/log", "/api/chat/context"):
            response = await h.post(path, payload)
            self.assertEqual((response.status_code, response.json()["code"]), (410, "retired"))
        await h.guard.drain_logs()
        self.assertEqual((h.logs, h.build_calls, h.upstream_calls), ([], [], 0))

    async def test_server_side_log_and_status_hold_no_user_text(self):
        h = self.harness()
        await h.session(**{"X-Real-IP": "203.0.113.77"})
        secret = dict(CHAT, query="私密检索词", user_message="私密提问内容")
        self.assertEqual((await h.post("/api/chat", secret, **{"X-Real-IP": "203.0.113.77"})).status_code, 200)
        await h.guard.drain_logs()
        self.assertEqual(len(h.logs), 1)
        status = (await h.client.get("/api/ai/status")).text
        for blob in (json.dumps(h.logs, ensure_ascii=False), status):
            for forbidden in ("私密检索词", "私密提问内容", "203.0.113.77", KEY, "PROMPT"):
                self.assertNotIn(forbidden, blob)
        self.assertEqual(h.logs[0]["kind"], "chat")

    async def test_disconnect_watcher_sets_gone(self):
        class FakeRequest:
            def __init__(self):
                self.messages = [{"type": "http.request", "body": b""}, {"type": "http.disconnect"}]

            async def receive(self):
                await asyncio.sleep(0)
                return self.messages.pop(0)

        gone = asyncio.Event()
        await ai_guard._watch_disconnect(FakeRequest(), gone)
        self.assertTrue(gone.is_set())


if __name__ == "__main__":
    unittest.main()
