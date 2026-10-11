"""Unit tests for backend/ai_guard.py. No network: every upstream is a mock."""
import asyncio
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import ai_guard  # noqa: E402
from ai_guard import AIGuardError, Built, BudgetStore, Guard, GuardConfig  # noqa: E402

KEY = "k" * 40
ENABLED_ENV = {"AI_ENABLED": "1", "AI_SESSION_KEY": KEY, "AI_DAILY_REQUEST_BUDGET": "100"}


def make_config(tmp: str, **env) -> GuardConfig:
    merged = dict(ENABLED_ENV)
    merged.update({k: str(v) for k, v in env.items()})
    return GuardConfig.from_env(merged, state_root=Path(tmp))


class FakeUpstream:
    """Counts calls and cancellations; optionally blocks until released."""

    def __init__(self, answer="答案", block=False):
        self.calls = 0
        self.cancelled = 0
        self.completed = 0
        self.answer = answer
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        if not block:
            self.release.set()

    async def __call__(self, prompt: str) -> str:
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        self.completed += 1
        return self.answer


def builder(prompt="提示", calls=None):
    async def build():
        if calls is not None:
            calls.append(1)
        return Built(prompt=prompt, finish=lambda answer: {"answer": answer}, meta={"evidence_count": 3})
    return build


class ConfigTests(unittest.TestCase):
    def test_disabled_by_default(self):
        cfg = GuardConfig.from_env({})
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.disabled_reasons(), ["AI_ENABLED", "AI_SESSION_KEY", "AI_DAILY_REQUEST_BUDGET"])
        self.assertFalse(GuardConfig().enabled)

    def test_all_three_settings_are_required(self):
        for missing in ENABLED_ENV:
            env = {k: v for k, v in ENABLED_ENV.items() if k != missing}
            cfg = GuardConfig.from_env(env)
            self.assertFalse(cfg.enabled, missing)
            self.assertEqual(cfg.disabled_reasons(), [missing])
        self.assertTrue(GuardConfig.from_env(ENABLED_ENV).enabled)

    def test_weak_or_odd_values_stay_disabled(self):
        self.assertFalse(GuardConfig.from_env({**ENABLED_ENV, "AI_SESSION_KEY": "short"}).enabled)
        self.assertFalse(GuardConfig.from_env({**ENABLED_ENV, "AI_ENABLED": "true"}).enabled)
        self.assertFalse(GuardConfig.from_env({**ENABLED_ENV, "AI_DAILY_REQUEST_BUDGET": "abc"}).enabled)
        self.assertFalse(GuardConfig.from_env({**ENABLED_ENV, "AI_DAILY_REQUEST_BUDGET": "0"}).enabled)

    def test_retry_and_deadline_are_clamped(self):
        cfg = GuardConfig.from_env({**ENABLED_ENV, "AI_SERVICE_RETRIES": "9", "AI_SERVICE_TIMEOUT_SEC": "120"})
        self.assertEqual(cfg.retries, 1)
        self.assertEqual(cfg.upstream_deadline_sec, 30.0)
        default = GuardConfig.from_env(ENABLED_ENV)
        self.assertEqual(default.retries, 0)
        self.assertLessEqual(default.upstream_deadline_sec, 30.0)


class SubjectTests(unittest.TestCase):
    LOOPBACK = ai_guard._parse_networks("127.0.0.0/8,::1/128")

    def test_x_real_ip_only_from_loopback_peer(self):
        self.assertEqual(ai_guard.client_subject("127.0.0.1", "203.0.113.9", self.LOOPBACK), "203.0.113.9")
        self.assertEqual(ai_guard.client_subject("::1", "203.0.113.9", self.LOOPBACK), "203.0.113.9")
        # A direct connection cannot choose its own subject.
        self.assertEqual(ai_guard.client_subject("198.51.100.7", "203.0.113.9", self.LOOPBACK), "198.51.100.7")
        self.assertEqual(ai_guard.client_subject("172.17.0.1", "203.0.113.9", self.LOOPBACK), "172.17.0.1")
        self.assertEqual(ai_guard.client_subject("127.0.0.1", "not-an-ip", self.LOOPBACK), "127.0.0.1")
        self.assertEqual(ai_guard.client_subject(None, "203.0.113.9", self.LOOPBACK), "unknown")

    def test_ipv6_is_grouped_by_64(self):
        a = ai_guard.client_subject("2001:db8:1:2:aaaa::1", None, self.LOOPBACK)
        b = ai_guard.client_subject("2001:db8:1:2:bbbb::9", None, self.LOOPBACK)
        c = ai_guard.client_subject("2001:db8:1:3::1", None, self.LOOPBACK)
        self.assertEqual(a, b)
        self.assertEqual(a, "2001:db8:1:2::/64")
        self.assertNotEqual(a, c)
        self.assertEqual(ai_guard.client_subject("::ffff:192.0.2.5", None, self.LOOPBACK), "192.0.2.5")


class SessionTokenTests(unittest.TestCase):
    def test_round_trip_and_rejections(self):
        key = KEY.encode()
        token, expires = ai_guard.issue_session_token(key, "192.0.2.1", 600, now=1000)
        self.assertEqual(expires, 1600)
        self.assertTrue(ai_guard.verify_session_token(key, token, "192.0.2.1", now=1001))
        self.assertFalse(ai_guard.verify_session_token(key, token, "192.0.2.2", now=1001), "bound to subject")
        self.assertFalse(ai_guard.verify_session_token(key, token, "192.0.2.1", now=1600), "expired")
        self.assertFalse(ai_guard.verify_session_token(b"x" * 40, token, "192.0.2.1", now=1001), "other key")
        self.assertFalse(ai_guard.verify_session_token(key, token[:-2] + "AA", "192.0.2.1", now=1001))
        parts = token.split(".")
        forged = ".".join([parts[0], "9999999999", parts[2], parts[3]])
        self.assertFalse(ai_guard.verify_session_token(key, forged, "192.0.2.1", now=1001), "expiry is signed")
        for junk in (None, "", "v1.x.y.z", "a.b", "v2.1.2.3", "x" * 500):
            self.assertFalse(ai_guard.verify_session_token(key, junk, "192.0.2.1", now=1001))
        self.assertFalse(ai_guard.verify_session_token(b"", token, "192.0.2.1", now=1001))

    def test_non_ascii_digits_in_expiry_are_rejected_not_raised(self):
        key = KEY.encode()
        # "²".isdigit() is True but int("²") raises: must be a clean False (review m1a).
        for token in ("v1.².x.y", "v1.１２３４.x.y", "v1.٣.x.y", "v1." + "9" * 400 + ".x.y"):
            self.assertFalse(ai_guard.verify_session_token(key, token, "192.0.2.1", now=1001), token[:12])


class RateLimiterTests(unittest.TestCase):
    def test_burst_then_refill(self):
        now = [0.0]
        limiter = ai_guard.RateLimiter(3, 6, clock=lambda: now[0])
        self.assertEqual([limiter.take("a") for _ in range(3)], [0.0, 0.0, 0.0])
        wait = limiter.take("a")
        self.assertAlmostEqual(wait, 10.0, places=3)
        self.assertEqual(limiter.take("b"), 0.0, "other subject unaffected")
        now[0] += 9.0
        self.assertGreater(limiter.take("a"), 0.0)
        now[0] += 1.5
        self.assertEqual(limiter.take("a"), 0.0)

    def test_subject_table_is_bounded(self):
        limiter = ai_guard.RateLimiter(1, 1, max_subjects=50, clock=lambda: 0.0)
        for index in range(500):
            limiter.take(f"s{index}")
        self.assertLessEqual(len(limiter._buckets), 50)


class ValidationTests(unittest.TestCase):
    cfg = GuardConfig.from_env(ENABLED_ENV)

    def code(self, fn, payload):
        with self.assertRaises(AIGuardError) as ctx:
            fn(payload, self.cfg)
        return ctx.exception.status, ctx.exception.code

    def test_chat_caps_reject_instead_of_truncating(self):
        ok = ai_guard.validate_chat({"query": " 牛顿第二定律 ", "user_message": "解释", "history": None, "extra": "x" * 10}, self.cfg)
        self.assertEqual(ok["query"], "牛顿第二定律")
        self.assertNotIn("extra", ok)
        self.assertEqual(self.code(ai_guard.validate_chat, {"query": "q" * 201, "user_message": "m"}), (413, "input_too_long"))
        self.assertEqual(self.code(ai_guard.validate_chat, {"query": "q", "user_message": "m" * 801}), (413, "input_too_long"))
        self.assertEqual(self.code(ai_guard.validate_chat, {"query": "", "user_message": "m"}), (400, "bad_request"))
        self.assertEqual(self.code(ai_guard.validate_chat, {"query": 5, "user_message": "m"}), (400, "bad_request"))
        self.assertEqual(self.code(ai_guard.validate_chat, ["query"]), (400, "bad_request"))
        self.assertEqual(self.code(ai_guard.validate_chat, {"query": "q", "user_message": "m", "phase": "大学"}), (400, "bad_request"))
        self.assertEqual(self.code(ai_guard.validate_chat, {"query": "q", "user_message": "m", "book_key": "b" * 201}), (413, "input_too_long"))

    def test_history_count_and_item_length(self):
        item = {"role": "user", "content": "x" * 1200}
        ok = ai_guard.validate_chat({"query": "q", "user_message": "m", "history": [item] * 6}, self.cfg)
        self.assertEqual(len(ok["history"]), 6)
        self.assertEqual(self.code(ai_guard.validate_chat, {"query": "q", "user_message": "m", "history": [item] * 7}), (413, "history_too_long"))
        long_item = {"role": "assistant", "content": "x" * 1201}
        self.assertEqual(self.code(ai_guard.validate_chat, {"query": "q", "user_message": "m", "history": [long_item]}), (413, "history_too_long"))
        for bad in ("text", [{"role": "system", "content": "x"}], [{"role": "user", "content": 3}], ["x"]):
            self.assertEqual(self.code(ai_guard.validate_chat, {"query": "q", "user_message": "m", "history": bad}), (400, "bad_request"))
        # Only role/content survive: rich client objects are not forwarded.
        rich = [{"role": "assistant", "content": "a", "sources": ["x"] * 50, "contextSummary": {"k": 1}}]
        self.assertEqual(ai_guard.validate_chat({"query": "q", "user_message": "m", "history": rich}, self.cfg)["history"], [{"role": "assistant", "content": "a"}])

    def test_dict_and_gaokao(self):
        ok = ai_guard.validate_dict_chat({"headword": "之", "user_message": "m", "dict_context": "x" * 9000}, self.cfg)
        self.assertEqual(set(ok), {"headword", "user_message", "history", "phase"})
        self.assertEqual(self.code(ai_guard.validate_dict_chat, {"headword": "字" * 41, "user_message": "m"}), (413, "input_too_long"))
        self.assertEqual(ai_guard.validate_gaokao_chat({"question_id": 12, "phase": "高中"}, self.cfg), {"question_id": 12, "phase": "高中"})
        for bad in ("12", True, -1, 0, 1.5, None):
            self.assertEqual(self.code(ai_guard.validate_gaokao_chat, {"question_id": bad}), (400, "bad_request"))

    def test_lone_surrogates_are_rejected_as_bad_request(self):
        # JSON "\ud800" decodes to a lone surrogate that cannot be encoded or hashed (review m1c).
        bad = "\ud800"
        cases = (
            (ai_guard.validate_chat, {"query": bad, "user_message": "m"}),
            (ai_guard.validate_chat, {"query": "q", "user_message": "x" + bad}),
            (ai_guard.validate_chat, {"query": "q", "user_message": "m", "scope_subject": bad}),
            (ai_guard.validate_chat, {"query": "q", "user_message": "m", "book_key": bad}),
            (ai_guard.validate_chat, {"query": "q", "user_message": "m", "history": [{"role": "user", "content": bad}]}),
            (ai_guard.validate_dict_chat, {"headword": bad, "user_message": "m"}),
        )
        for fn, payload in cases:
            self.assertEqual(self.code(fn, payload), (400, "bad_request"))
        # Defence in depth: the hashing helpers themselves no longer raise on such text.
        ai_guard.dedup_key("chat", {"query": bad})

    def test_dedup_ignores_invisible_format_characters(self):
        base = {"query": "晶体的定义", "user_message": "解释"}
        for variant in ("晶体\u200b的定义", "\ufeff晶体的定义", "晶体的\u200d定义\u200e"):
            self.assertEqual(ai_guard.dedup_key("chat", base), ai_guard.dedup_key("chat", dict(base, query=variant)))
        self.assertNotEqual(ai_guard.dedup_key("chat", base), ai_guard.dedup_key("chat", dict(base, query="晶体的定义。")))

    def test_dedup_key_normalisation(self):
        base = {"query": "牛顿 第二定律", "user_message": "解释", "history": [], "phase": "高中"}
        same = {"query": "  牛顿　\n第二定律 ", "user_message": "解释", "history": [], "phase": "高中"}
        other = {"query": "牛顿第三定律", "user_message": "解释", "history": [], "phase": "高中"}
        self.assertEqual(ai_guard.dedup_key("chat", base), ai_guard.dedup_key("chat", same))
        self.assertNotEqual(ai_guard.dedup_key("chat", base), ai_guard.dedup_key("chat", other))
        self.assertNotEqual(ai_guard.dedup_key("chat", base), ai_guard.dedup_key("dict", base))


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ai_guard" / "ai_budget.sqlite3"
        self.now = [datetime(2026, 10, 10, 23, 59, 0, tzinfo=timezone.utc)]

    def tearDown(self):
        self.tmp.cleanup()

    def store(self):
        return BudgetStore(self.path, utcnow=lambda: self.now[0])

    def test_hard_stop_restart_and_date_reset(self):
        store = self.store()
        self.assertEqual([store.consume(3) for _ in range(3)], [1, 2, 3])
        with self.assertRaises(AIGuardError) as ctx:
            store.consume(3)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (503, "AI_BUDGET_EXHAUSTED"))
        self.assertEqual(ctx.exception.retry_after, 60)
        # "Process restart": a brand-new store object on the same file still refuses.
        restarted = self.store()
        with self.assertRaises(AIGuardError):
            restarted.consume(3)
        snapshot = restarted.snapshot(3)
        self.assertEqual((snapshot["date"], snapshot["count"], snapshot["limit"], snapshot["remaining"]), ("2026-10-10", 3, 3, 0))
        self.assertEqual(snapshot["resets_at"], "2026-10-11T00:00:00Z")
        # UTC date change resets the counter.
        self.now[0] = datetime(2026, 10, 11, 0, 0, 1, tzinfo=timezone.utc)
        self.assertEqual(restarted.consume(3), 1)
        self.assertEqual(restarted.snapshot(3)["remaining"], 2)

    def test_atomic_under_threads(self):
        store = self.store()
        granted, refused = [], []

        def worker():
            try:
                granted.append(store.consume(25))
            except AIGuardError:
                refused.append(1)

        threads = [threading.Thread(target=worker) for _ in range(60)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(granted), 25)
        self.assertEqual(len(refused), 35)
        self.assertEqual(sorted(granted), list(range(1, 26)))

    def test_subject_cap_does_not_spend_global_budget(self):
        store = self.store()
        store.consume(10, subject_hash="a", subject_cap=2)
        store.consume(10, subject_hash="a", subject_cap=2)
        with self.assertRaises(AIGuardError) as ctx:
            store.consume(10, subject_hash="a", subject_cap=2)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (429, "AI_SUBJECT_BUDGET_EXHAUSTED"))
        self.assertEqual(store.snapshot(10)["count"], 2)
        self.assertEqual(store.consume(10, subject_hash="b", subject_cap=2), 3)

    def test_storage_failure_fails_closed(self):
        blocker = Path(self.tmp.name) / "file"
        blocker.write_text("x")
        store = BudgetStore(blocker / "nested" / "budget.sqlite3")
        with self.assertRaises(AIGuardError) as ctx:
            store.consume(5)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (503, "AI_BUDGET_UNAVAILABLE"))
        self.assertFalse(store.snapshot(5)["available"])

    def test_zero_limit_never_grants(self):
        with self.assertRaises(AIGuardError) as ctx:
            self.store().consume(0)
        self.assertEqual(ctx.exception.code, "AI_DISABLED")


class UpstreamTests(unittest.IsolatedAsyncioTestCase):
    URL = "https://upstream.invalid/"

    async def call(self, handler, **kwargs):
        options = {"deadline_sec": 5, "max_bytes": 4096, "retries": 0}
        options.update(kwargs)
        return await ai_guard.post_upstream(self.URL, {"prompt": "p"}, {"X-Test": "1"}, transport=httpx.MockTransport(handler), **options)

    async def test_answer_shapes(self):
        self.assertEqual(await self.call(lambda r: httpx.Response(200, json={"answer": " 好 "})), "好")
        self.assertEqual(await self.call(lambda r: httpx.Response(200, json={"data": {"answer": "好"}})), "好")
        for body in ({"answer": ""}, {"error": "x"}, [1], "text"):
            with self.assertRaises(AIGuardError) as ctx:
                await self.call(lambda r, b=body: httpx.Response(200, json=b))
            self.assertEqual((ctx.exception.status, ctx.exception.code), (502, "ai_empty"))

    async def test_no_retry_after_timeout(self):
        calls = []

        def handler(request):
            calls.append(1)
            raise httpx.ReadTimeout("slow", request=request)

        with self.assertRaises(AIGuardError) as ctx:
            await self.call(handler, retries=1)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (504, "ai_timeout"))
        self.assertEqual(len(calls), 1)

    async def test_no_retry_after_connect_timeout_or_http_error(self):
        for make in (
            lambda request: (_ for _ in ()).throw(httpx.ConnectTimeout("t", request=request)),
            lambda request: httpx.Response(500, text="boom"),
            lambda request: httpx.Response(503, text="boom"),
            lambda request: (_ for _ in ()).throw(httpx.ReadError("reset", request=request)),
        ):
            calls = []

            def handler(request, make=make):
                calls.append(1)
                return make(request)

            with self.assertRaises(AIGuardError):
                await self.call(handler, retries=1)
            self.assertEqual(len(calls), 1)

    async def test_connect_error_retry_is_opt_in_and_capped_at_one(self):
        for retries, expected in ((0, 1), (1, 2), (7, 2)):
            calls = []

            def handler(request):
                calls.append(1)
                raise httpx.ConnectError("refused", request=request)

            with self.assertRaises(AIGuardError) as ctx:
                await self.call(handler, retries=retries)
            self.assertEqual(ctx.exception.code, "ai_unreachable")
            self.assertEqual(len(calls), expected, retries)

    async def test_total_deadline_is_single_attempt(self):
        calls = []

        async def handler(request):
            calls.append(1)
            await asyncio.sleep(5)
            return httpx.Response(200, json={"answer": "late"})

        started = asyncio.get_running_loop().time()
        with self.assertRaises(AIGuardError) as ctx:
            await self.call(handler, deadline_sec=1, retries=1)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (504, "ai_timeout"))
        self.assertEqual(len(calls), 1)
        self.assertLess(asyncio.get_running_loop().time() - started, 2.5)

    async def test_deadline_cannot_exceed_thirty_seconds(self):
        seen = {}
        real = httpx.Timeout

        def spy(value, *a, **k):
            seen["timeout"] = value
            return real(value, *a, **k)

        httpx.Timeout = spy
        try:
            await self.call(lambda r: httpx.Response(200, json={"answer": "a"}), deadline_sec=600)
        finally:
            httpx.Timeout = real
        self.assertEqual(seen["timeout"], 30.0)

    async def test_output_byte_cap(self):
        big = json.dumps({"answer": "x" * 10000}).encode()
        with self.assertRaises(AIGuardError) as ctx:
            await self.call(lambda r: httpx.Response(200, content=big))
        self.assertEqual((ctx.exception.status, ctx.exception.code), (502, "ai_oversize"))

        async def chunks():
            for _ in range(100):
                yield b"y" * 1024

        with self.assertRaises(AIGuardError) as ctx:
            await self.call(lambda r: httpx.Response(200, content=chunks()))
        self.assertEqual(ctx.exception.code, "ai_oversize")

    async def test_upstream_error_text_is_not_leaked(self):
        with self.assertRaises(AIGuardError) as ctx:
            await self.call(lambda r: httpx.Response(400, text="SECRET upstream detail"))
        self.assertNotIn("SECRET", ctx.exception.message)
        self.assertNotIn("SECRET", json.dumps(ctx.exception.body(), ensure_ascii=False))
        with self.assertRaises(AIGuardError) as ctx:
            await self.call(lambda r: httpx.Response(429, headers={"Retry-After": "7"}))
        self.assertEqual((ctx.exception.status, ctx.exception.retry_after), (429, 7))
        # Retry-After "²": isdigit() is True, float() raises — must stay a clean 429 (review m1d).
        for odd in ("²", "１０", "soon", ""):
            with self.assertRaises(AIGuardError) as ctx:
                await self.call(lambda r, v=odd: httpx.Response(429, headers=[(b"Retry-After", v.encode("utf-8"))]))
            self.assertEqual((ctx.exception.status, ctx.exception.code, ctx.exception.retry_after), (429, "ai_upstream", 30))

    def test_answer_char_cap(self):
        self.assertEqual(ai_guard.cap_answer("短", 10), "短")
        capped = ai_guard.cap_answer("字" * 5000, 4000)
        self.assertTrue(capped.endswith(ai_guard.ANSWER_TRUNCATION_MARK))
        self.assertEqual(len(capped), 4000 + len(ai_guard.ANSWER_TRUNCATION_MARK))


class GuardFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def guard(self, upstream, clock=None, **env):
        cfg = make_config(self.tmp.name, **env)
        kwargs = {"clock": clock} if clock else {}
        return Guard(cfg, upstream=upstream, log=self.logs.append, **kwargs)

    FIELDS = {"query": "牛顿第二定律", "user_message": "解释", "history": [], "phase": "高中"}

    async def test_three_identical_concurrent_requests_share_one_upstream_call(self):
        upstream = FakeUpstream(block=True)
        guard = self.guard(upstream)
        builds = []
        tasks = [asyncio.ensure_future(guard.handle("chat", self.FIELDS, "s", builder(calls=builds))) for _ in range(3)]
        await upstream.started.wait()
        await asyncio.sleep(0)
        self.assertEqual(len(guard.inflight), 1)
        upstream.release.set()
        results = await asyncio.gather(*tasks)
        self.assertEqual(upstream.calls, 1)
        self.assertEqual(len(builds), 1, "retrieval also runs once")
        self.assertEqual([r["answer"] for r in results], ["答案"] * 3)
        self.assertEqual(guard.inflight, {})
        self.assertEqual(guard.budget.snapshot(100)["count"], 1)

    async def test_completed_answer_is_served_from_cache_until_ttl(self):
        now = [100.0]
        upstream = FakeUpstream()
        guard = self.guard(upstream, clock=lambda: now[0], AI_CACHE_TTL_SEC=600)
        first = await guard.handle("chat", self.FIELDS, "s", builder())
        spaced = dict(self.FIELDS, query=" 牛顿第二定律  ")
        second = await guard.handle("chat", spaced, "s", builder())
        self.assertEqual((first["cached"], second["cached"]), (False, True))
        self.assertEqual(upstream.calls, 1)
        now[0] += 601
        third = await guard.handle("chat", self.FIELDS, "s", builder())
        self.assertFalse(third["cached"])
        self.assertEqual(upstream.calls, 2)

    async def test_cache_is_bounded_and_failures_are_not_cached(self):
        upstream = FakeUpstream()
        guard = self.guard(upstream, AI_CACHE_MAX_ENTRIES=3)
        for index in range(10):
            await guard.handle("chat", dict(self.FIELDS, query=f"q{index}"), "s", builder())
        self.assertEqual(len(guard._cache), 3)

        async def failing(prompt):
            raise AIGuardError(504, "ai_timeout")

        guard2 = self.guard(failing)
        for _ in range(2):
            with self.assertRaises(AIGuardError):
                await guard2.handle("chat", self.FIELDS, "s", builder())
        self.assertEqual(len(guard2._cache), 0)
        self.assertEqual(guard2.counters["upstream_calls"], 2, "manual retry makes a new call; nothing automatic")

    async def test_one_of_two_waiters_leaves_upstream_continues(self):
        for leaver in (0, 1):  # creator leaves / joiner leaves
            upstream = FakeUpstream(block=True)
            guard = self.guard(upstream, AI_CACHE_TTL_SEC=0)
            gone = [asyncio.Event(), asyncio.Event()]
            tasks = [asyncio.ensure_future(guard.handle("chat", self.FIELDS, "s", builder(), gone=gone[i])) for i in range(2)]
            await upstream.started.wait()
            await asyncio.sleep(0)
            gone[leaver].set()
            with self.assertRaises(AIGuardError) as ctx:
                await tasks[leaver]
            self.assertEqual((ctx.exception.status, ctx.exception.code), (499, "cancelled"))
            self.assertEqual(upstream.cancelled, 0)
            self.assertEqual(len(guard.inflight), 1)
            upstream.release.set()
            self.assertEqual((await tasks[1 - leaver])["answer"], "答案")
            self.assertEqual((upstream.calls, upstream.cancelled, upstream.completed), (1, 0, 1))

    async def test_all_waiters_leave_upstream_cancelled_exactly_once(self):
        upstream = FakeUpstream(block=True)
        guard = self.guard(upstream)
        gone = [asyncio.Event(), asyncio.Event()]
        tasks = [asyncio.ensure_future(guard.handle("chat", self.FIELDS, "s", builder(), gone=gone[i])) for i in range(2)]
        await upstream.started.wait()
        await asyncio.sleep(0)
        gone[0].set()
        with self.assertRaises(AIGuardError):
            await tasks[0]
        self.assertEqual(upstream.cancelled, 0)
        gone[1].set()
        with self.assertRaises(AIGuardError):
            await tasks[1]
        self.assertEqual(guard.inflight, {}, "removed synchronously on last-waiter exit")
        await asyncio.sleep(0.01)
        self.assertEqual((upstream.calls, upstream.cancelled, upstream.completed), (1, 1, 0))
        self.assertEqual(guard._active, 0)
        self.assertFalse(guard._sem.locked())
        await guard.drain_logs()
        self.assertEqual(self.logs[-1]["outcome"], "cancelled")

    async def test_cancel_then_identical_request_makes_exactly_one_new_call(self):
        upstream = FakeUpstream(block=True)
        guard = self.guard(upstream)
        gone = asyncio.Event()
        first = asyncio.ensure_future(guard.handle("chat", self.FIELDS, "s", builder(), gone=gone))
        await upstream.started.wait()
        gone.set()
        with self.assertRaises(AIGuardError):
            await first
        # Arrives before the cancelled call has finished unwinding: it must not join the dying entry.
        second = asyncio.ensure_future(guard.handle("chat", self.FIELDS, "s", builder()))
        third = asyncio.ensure_future(guard.handle("chat", self.FIELDS, "s", builder()))
        await asyncio.sleep(0.01)
        upstream.release.set()
        results = await asyncio.gather(second, third)
        self.assertEqual([r["answer"] for r in results], ["答案", "答案"])
        self.assertEqual((upstream.calls, upstream.cancelled, upstream.completed), (2, 1, 1))

    async def test_waiter_task_cancellation_also_releases(self):
        upstream = FakeUpstream(block=True)
        guard = self.guard(upstream)
        task = asyncio.ensure_future(guard.handle("chat", self.FIELDS, "s", builder()))
        await upstream.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.01)
        self.assertEqual((upstream.calls, upstream.cancelled), (1, 1))
        self.assertEqual(guard.inflight, {})

    async def test_already_gone_request_starts_nothing(self):
        upstream = FakeUpstream()
        guard = self.guard(upstream)
        gone = asyncio.Event()
        gone.set()
        builds = []
        with self.assertRaises(AIGuardError):
            await guard.handle("chat", self.FIELDS, "s", builder(calls=builds), gone=gone)
        await asyncio.sleep(0.01)
        self.assertEqual((upstream.calls, len(builds), guard.inflight), (0, 0, {}))

    async def test_budget_stop_after_n_upstream_calls(self):
        upstream = FakeUpstream()
        guard = self.guard(upstream, AI_DAILY_REQUEST_BUDGET=3, AI_CACHE_TTL_SEC=0)
        for index in range(3):
            await guard.handle("chat", dict(self.FIELDS, query=f"q{index}"), "s", builder())
        for index in range(3, 6):
            with self.assertRaises(AIGuardError) as ctx:
                await guard.handle("chat", dict(self.FIELDS, query=f"q{index}"), "s", builder())
            self.assertEqual((ctx.exception.status, ctx.exception.code), (503, "AI_BUDGET_EXHAUSTED"))
        self.assertEqual(upstream.calls, 3)
        # A new Guard (process restart) on the same STATE_ROOT is still stopped.
        upstream2 = FakeUpstream()
        guard2 = self.guard(upstream2, AI_DAILY_REQUEST_BUDGET=3)
        with self.assertRaises(AIGuardError):
            await guard2.handle("chat", self.FIELDS, "s", builder())
        self.assertEqual(upstream2.calls, 0)
        self.assertEqual(guard2.status()["budget"]["remaining"], 0)

    async def test_budget_is_spent_before_the_call_and_not_refunded(self):
        async def failing(prompt):
            raise AIGuardError(504, "ai_timeout")

        guard = self.guard(failing, AI_DAILY_REQUEST_BUDGET=2, AI_CACHE_TTL_SEC=0)
        for _ in range(2):
            with self.assertRaises(AIGuardError) as ctx:
                await guard.handle("chat", self.FIELDS, "s", builder())
            self.assertEqual(ctx.exception.code, "ai_timeout")
        with self.assertRaises(AIGuardError) as ctx:
            await guard.handle("chat", self.FIELDS, "s", builder())
        self.assertEqual(ctx.exception.code, "AI_BUDGET_EXHAUSTED")

    async def test_exhausted_budget_is_refused_before_retrieval(self):
        upstream = FakeUpstream()
        guard = self.guard(upstream, AI_DAILY_REQUEST_BUDGET=1, AI_CACHE_TTL_SEC=0)
        builds = []
        await guard.handle("chat", self.FIELDS, "s", builder(calls=builds))
        for index in range(3):
            with self.assertRaises(AIGuardError) as ctx:
                await guard.handle("chat", dict(self.FIELDS, query=f"n{index}"), "s", builder(calls=builds))
            self.assertEqual(ctx.exception.code, "AI_BUDGET_EXHAUSTED")
        self.assertEqual((len(builds), upstream.calls), (1, 1), "no retrieval once the budget is gone (review m6)")
        self.assertEqual(guard.counters["budget_refused"], 3)

    async def test_unavailable_budget_store_is_refused_before_retrieval(self):
        blocker = Path(self.tmp.name) / "plainfile"
        blocker.write_text("x")
        cfg = make_config(self.tmp.name)
        upstream = FakeUpstream()
        guard = Guard(cfg, upstream=upstream, budget=BudgetStore(blocker / "x" / "b.sqlite3"))
        builds = []
        with self.assertRaises(AIGuardError) as ctx:
            await guard.handle("chat", self.FIELDS, "s", builder(calls=builds))
        self.assertEqual((ctx.exception.status, ctx.exception.code), (503, "AI_BUDGET_UNAVAILABLE"))
        self.assertEqual((len(builds), upstream.calls), (0, 0))

    async def test_status_reads_the_budget_store_at_most_once_per_window(self):
        now = [0.0]
        guard = self.guard(FakeUpstream(), clock=lambda: now[0])
        reads = []
        real = guard.budget.snapshot
        guard.budget.snapshot = lambda limit: (reads.append(1), real(limit))[1]
        for _ in range(50):
            guard.status()
        self.assertEqual(len(reads), 1, "polling the public status endpoint does not hit SQLite each time (review m10)")
        now[0] += ai_guard.STATUS_CACHE_SEC + 0.1
        guard.status()
        self.assertEqual(len(reads), 2)
        await guard.handle("chat", self.FIELDS, "s", builder())   # a reservation invalidates the cached view
        self.assertEqual(guard.status()["budget"]["count"], 1)

    async def test_prompt_cap_rejects_before_budget_and_upstream(self):
        upstream = FakeUpstream()
        guard = self.guard(upstream, AI_MAX_PROMPT_CHARS=1000)
        with self.assertRaises(AIGuardError) as ctx:
            await guard.handle("chat", self.FIELDS, "s", builder(prompt="长" * 1001))
        self.assertEqual((ctx.exception.status, ctx.exception.code), (413, "prompt_too_large"))
        self.assertEqual(upstream.calls, 0)
        self.assertEqual(guard.budget.snapshot(100)["count"], 0)
        ok = await guard.handle("chat", dict(self.FIELDS, query="b"), "s", builder(prompt="长" * 1000))
        self.assertEqual(ok["answer"], "答案")

    async def test_answer_is_capped(self):
        guard = self.guard(FakeUpstream(answer="字" * 9000), AI_MAX_ANSWER_CHARS=500)
        result = await guard.handle("chat", self.FIELDS, "s", builder())
        self.assertEqual(len(result["answer"]), 500 + len(ai_guard.ANSWER_TRUNCATION_MARK))

    async def test_concurrency_saturation(self):
        upstream = FakeUpstream(block=True)
        guard = self.guard(upstream, AI_MAX_CONCURRENT=1, AI_MAX_QUEUE=1, AI_QUEUE_WAIT_SEC=30)
        running = asyncio.ensure_future(guard.handle("chat", dict(self.FIELDS, query="a"), "s", builder()))
        await upstream.started.wait()
        queued = asyncio.ensure_future(guard.handle("chat", dict(self.FIELDS, query="b"), "s", builder()))
        await asyncio.sleep(0.01)
        self.assertEqual((guard._active, guard._queued), (1, 1))
        with self.assertRaises(AIGuardError) as ctx:
            await guard.handle("chat", dict(self.FIELDS, query="c"), "s", builder())
        self.assertEqual((ctx.exception.status, ctx.exception.code, ctx.exception.retry_after), (503, "AI_BUSY", 5))
        self.assertEqual(upstream.calls, 1)
        upstream.release.set()
        await asyncio.gather(running, queued)
        self.assertEqual(upstream.calls, 2)
        self.assertEqual((guard._active, guard._queued), (0, 0))

    async def test_queue_wait_times_out(self):
        upstream = FakeUpstream(block=True)
        guard = self.guard(upstream, AI_MAX_CONCURRENT=1, AI_MAX_QUEUE=2, AI_QUEUE_WAIT_SEC=0.05)
        running = asyncio.ensure_future(guard.handle("chat", dict(self.FIELDS, query="a"), "s", builder()))
        await upstream.started.wait()
        with self.assertRaises(AIGuardError) as ctx:
            await guard.handle("chat", dict(self.FIELDS, query="b"), "s", builder())
        self.assertEqual(ctx.exception.code, "AI_BUSY")
        self.assertEqual(upstream.calls, 1)
        upstream.release.set()
        await running

    async def test_rate_limit_and_session_helpers(self):
        now = [0.0]
        guard = self.guard(FakeUpstream(), clock=lambda: now[0], AI_RATE_BURST=2, AI_RATE_PER_MINUTE=6)
        guard.rate_limit("a")
        guard.rate_limit("a")
        with self.assertRaises(AIGuardError) as ctx:
            guard.rate_limit("a")
        self.assertEqual((ctx.exception.status, ctx.exception.code, ctx.exception.retry_after), (429, "rate_limited", 10))
        guard.rate_limit("b")
        now[0] += 10
        guard.rate_limit("a")
        token, _ = guard.issue_session("a")
        guard.require_session(token, "a")
        with self.assertRaises(AIGuardError) as ctx:
            guard.require_session(token, "b")
        self.assertEqual((ctx.exception.status, ctx.exception.code), (401, "AI_SESSION_REQUIRED"))

    async def test_origin_check(self):
        guard = self.guard(FakeUpstream())
        guard.check_origin("https://sun.bdfz.net", "same-origin")
        guard.check_origin("https://sun.bdfz.net", None)
        for origin, site in ((None, None), ("https://evil.example", None), ("https://sun.bdfz.net.evil.example", None),
                             ("http://sun.bdfz.net", None), ("https://sun.bdfz.net", "cross-site"),
                             ("https://sun.bdfz.net", "same-site"), ("null", None), ("https://jks.bdfz.net", None)):
            with self.assertRaises(AIGuardError) as ctx:
                guard.check_origin(origin, site)
            self.assertEqual((ctx.exception.status, ctx.exception.code), (403, "forbidden_origin"))

    async def test_log_record_has_no_raw_text(self):
        guard = self.guard(FakeUpstream())
        secret_q, secret_m = "我的隐私检索词", "这是一段不应落库的提问"
        await guard.handle("chat", dict(self.FIELDS, query=secret_q, user_message=secret_m), "203.0.113.5", builder(),
                           primary_text=secret_q, message_text=secret_m)
        await guard.drain_logs()
        self.assertEqual(len(self.logs), 1)
        blob = json.dumps(self.logs[0], ensure_ascii=False)
        for forbidden in (secret_q, secret_m, "203.0.113.5", "提示", "答案"):
            self.assertNotIn(forbidden, blob)
        record = self.logs[0]
        self.assertEqual((record["outcome"], record["query_len"], record["message_len"], record["upstream_called"]),
                         ("ok", len(secret_q), len(secret_m), True))
        self.assertEqual(len(record["query_hash"]), 16)
        status = json.dumps(guard.status(), ensure_ascii=False)
        for forbidden in (secret_q, secret_m, "203.0.113.5", KEY):
            self.assertNotIn(forbidden, status)

    async def test_internal_errors_are_masked(self):
        async def broken():
            raise RuntimeError("db path /secret/place")

        guard = self.guard(FakeUpstream())
        with self.assertRaises(AIGuardError) as ctx:
            await guard.handle("chat", self.FIELDS, "s", broken)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (500, "internal_error"))
        self.assertNotIn("secret", ctx.exception.message)


LEGACY_SCHEMA = """
CREATE TABLE ai_chat_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    user_message TEXT NOT NULL,
    subject_count INTEGER DEFAULT 0,
    evidence_count INTEGER DEFAULT 0,
    gaokao_hit_count INTEGER DEFAULT 0,
    provider TEXT,
    phase TEXT,
    success INTEGER DEFAULT 0,
    error TEXT,
    ts REAL NOT NULL
)
"""


class ChatLogTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute(LEGACY_SCHEMA)
        self.con.execute(
            "INSERT INTO ai_chat_logs (query, user_message, provider, success, error, ts) VALUES ('旧检索', '旧提问', 'Gemini', 0, 'AI service http error: 500 raw', 1000.0)"
        )

    def tearDown(self):
        self.con.close()

    def test_additive_migration_keeps_legacy_rows(self):
        before = [row[1] for row in self.con.execute("PRAGMA table_info(ai_chat_logs)")]
        added = ai_guard.ensure_chat_log_columns(self.con)
        after = [row[1] for row in self.con.execute("PRAGMA table_info(ai_chat_logs)")]
        self.assertEqual(after[: len(before)], before, "no legacy column dropped, renamed or reordered")
        self.assertEqual(added, [name for name, _ in ai_guard.AI_CHAT_LOG_ADDITIVE_COLUMNS])
        self.assertEqual(ai_guard.ensure_chat_log_columns(self.con), [], "idempotent")
        self.assertEqual(self.con.execute("SELECT query, user_message FROM ai_chat_logs").fetchone(), ("旧检索", "旧提问"))

    def test_new_rows_store_no_raw_text(self):
        ai_guard.ensure_chat_log_columns(self.con)
        ai_guard.write_chat_log(
            self.con,
            {"kind": "chat", "outcome": "ai_timeout", "query_hash": "abcd", "query_len": 6, "message_len": 20,
             "prompt_chars": 3000, "answer_chars": 0, "duration_ms": 30000, "upstream_called": True,
             "evidence_count": 5, "phase": "高中", "query": "不应写入", "user_message": "不应写入"},
            provider="Gemini",
            now=2000.0,
        )
        self.con.row_factory = sqlite3.Row
        row = dict(self.con.execute("SELECT * FROM ai_chat_logs WHERE ts = 2000.0").fetchone())
        self.assertEqual((row["query"], row["user_message"]), ("", ""))
        self.assertEqual((row["success"], row["error"], row["outcome"], row["kind"]), (0, "ai_timeout", "ai_timeout", "chat"))
        self.assertEqual((row["query_len"], row["message_len"], row["prompt_chars"], row["evidence_count"]), (6, 20, 3000, 5))
        self.assertNotIn("不应写入", json.dumps(row, ensure_ascii=False))

    def test_prune_is_dry_run_by_default_and_bounded(self):
        for index in range(30):
            self.con.execute("INSERT INTO ai_chat_logs (query, user_message, ts) VALUES ('', '', ?)", (2000.0 + index,))
        now = 2000.0 + 400 * 86400
        dry = ai_guard.prune_chat_logs(self.con, older_than_days=180, now=now)
        self.assertEqual((dry["eligible"], dry["deleted"], dry["applied"]), (31, 0, False))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM ai_chat_logs").fetchone()[0], 31)
        applied = ai_guard.prune_chat_logs(self.con, older_than_days=180, limit=10, apply=True, now=now)
        self.assertEqual(applied["deleted"], 10)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM ai_chat_logs").fetchone()[0], 21)
        recent = ai_guard.prune_chat_logs(self.con, older_than_days=180, apply=True, now=2000.0)
        self.assertEqual(recent["deleted"], 0)
        with self.assertRaises(ValueError):
            ai_guard.prune_chat_logs(self.con, older_than_days=0, apply=True)

    def test_legacy_redaction_is_explicit(self):
        dry = ai_guard.redact_legacy_chat_log_text(self.con)
        self.assertEqual((dry["eligible"], dry["changed"]), (1, 0))
        self.assertEqual(self.con.execute("SELECT query FROM ai_chat_logs").fetchone()[0], "旧检索")
        done = ai_guard.redact_legacy_chat_log_text(self.con, apply=True)
        self.assertEqual(done["changed"], 1)
        self.assertEqual(self.con.execute("SELECT query, user_message, error, query_len FROM ai_chat_logs").fetchone(), ("", "", "legacy_error", 3))


class CitationTests(unittest.TestCase):
    EVIDENCE = [
        {"id": 1, "subject": "物理", "title": "必修第一册", "book_key": "bk1", "section": 87, "logical_page": 92,
         "citation": "[物理·必修第一册·p92]", "snippet": "牛顿第二定律", "text": "FULL " * 500, "final_score": 1.2},
    ]

    def test_sanitize_keeps_only_retrieval_fields(self):
        clean = ai_guard.sanitize_evidence(self.EVIDENCE + [{"snippet": "s" * 5000}, "junk"])
        self.assertEqual(len(clean), 2)
        self.assertNotIn("text", clean[0])
        self.assertNotIn("final_score", clean[0])
        self.assertEqual((clean[0]["book_key"], clean[0]["section"], clean[0]["logical_page"]), ("bk1", 87, 92))
        self.assertEqual(len(clean[1]["snippet"]), 600)
        self.assertEqual(len(ai_guard.sanitize_evidence(self.EVIDENCE * 20)), 8)

    def test_model_written_pages_are_only_reported(self):
        answer = "见 [物理·必修第一册·p92]，另见 [物理·必修第一册·p999] 和 [化学·选修三·p5]。"
        check = ai_guard.check_citations(answer, ai_guard.sanitize_evidence(self.EVIDENCE))
        self.assertEqual((check["found"], check["unverified_count"]), (3, 2))
        self.assertIn("[物理·必修第一册·p999]", check["unverified"])
        self.assertEqual(ai_guard.check_citations("没有出处", [])["found"], 0)


sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_test_timeout  # noqa: E402

ai_test_timeout.protect(globals())

if __name__ == "__main__":
    unittest.main()
