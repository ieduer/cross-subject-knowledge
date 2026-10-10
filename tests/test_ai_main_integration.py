"""Integration of the real backend/main.py with ai_guard.

main.py's heavy dependencies (torch, FAISS, jieba, the runtime corpus) are all
optional at import time, so the real FastAPI app can be imported against an
empty temporary DATA_ROOT. Retrieval is replaced by synthetic fixtures and the
upstream by a counter: no model endpoint is ever contacted.
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://sun.bdfz.net"
HEADERS = {"Origin": ORIGIN, "Content-Type": "application/json", "Sec-Fetch-Site": "same-origin", "X-Sun-AI-Client": "2"}
OLD_CLIENT_HEADERS = {"Origin": ORIGIN, "Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}
LEGACY = json.loads((Path(__file__).resolve().parent / "fixtures" / "ai_legacy_replies.json").read_text(encoding="utf-8"))
# This test must NEVER load an embedding / reranker model, whatever the interpreter has installed
# and whatever index files the checkout contains. Importing these names is made to fail, so
# main.py takes its dependency-free path (FAISS_AVAILABLE = False) by construction.
HEAVY_MODULES = ("faiss", "sentence_transformers", "torch", "transformers")
_SAVED_MODULES = {}
KEY = "integration-key-" + "q" * 40
_TMP = None
main = None
_SAVED_ENV = {}


def setUpModule():
    global _TMP, main
    _TMP = tempfile.TemporaryDirectory()
    root = Path(_TMP.name)
    (root / "data" / "index").mkdir(parents=True)
    env = {
        "DATA_ROOT": str(root / "data"), "STATE_ROOT": str(root / "state"), "PROJECT_ROOT": str(ROOT),
        "RERANKER_ENABLED": "0", "RERANKER_PRELOAD": "0", "RUNTIME_DB_SYNC_MODE": "disabled",
        "SUPPLEMENTAL_VECTOR_ENABLED": "0", "GRAPH_RAG_ENABLED": "0",
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HOME": str(root / "hf-empty"),
        "AI_ENABLED": "1", "AI_SESSION_KEY": KEY, "AI_DAILY_REQUEST_BUDGET": "5", "AI_RATE_BURST": "50",
        # A non-routable placeholder: the upstream function is replaced below and never used.
        "AI_SERVICE_URL": "http://127.0.0.1:9/never-called",
    }
    for name, value in env.items():
        _SAVED_ENV[name] = os.environ.get(name)
        os.environ[name] = value
    sys.path.insert(0, str(ROOT / "backend"))
    if "main" in sys.modules:
        _restore()
        raise unittest.SkipTest("a module named 'main' is already imported in this process; run this file on its own")
    for name in HEAVY_MODULES:
        _SAVED_MODULES[name] = sys.modules.get(name, _ABSENT)
        sys.modules[name] = None          # `import faiss` now raises ImportError
    try:
        import main as imported  # noqa: WPS433
    except Exception as exc:  # pragma: no cover - environment without fastapi etc.
        _restore()
        raise unittest.SkipTest(f"backend/main.py is not importable here: {exc!r}")
    loaded = [name for name in ("faiss_index", "supplemental_faiss_index", "embedder", "reranker") if getattr(imported, name, None) is not None]
    if getattr(imported, "FAISS_AVAILABLE", False) or loaded:
        _restore()
        raise unittest.SkipTest(f"refusing to run: main.py took a model-loading path (FAISS_AVAILABLE or {loaded})")
    main = imported


_ABSENT = object()


def _restore():
    for name, value in _SAVED_MODULES.items():
        if value is _ABSENT:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = value
    _SAVED_MODULES.clear()
    for name, value in _SAVED_ENV.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    _SAVED_ENV.clear()


def tearDownModule():
    _restore()
    sys.modules.pop("main", None)
    if _TMP is not None:
        _TMP.cleanup()


def fake_chat_context(query, user_message, history=None, *, scope_subject=None, book_key=None, phase=None):
    evidence = [{
        "id": 11, "subject": "物理", "title": "必修第一册", "book_key": "physics_b1", "section": 87, "logical_page": 92,
        "snippet": "物体加速度的大小跟它受到的作用力成正比。", "citation": "[物理·必修第一册·p92]", "text": "RAW " * 400,
    }]
    return {
        "mode": "cross_subject", "query": query, "user_message": user_message, "evidence": evidence,
        "context_text": "【物理】（1条命中）\n[物理·必修第一册·p92] 物体加速度的大小跟它受到的作用力成正比。",
        "gaokao_text": "（无）", "history_text": "\n".join(main._format_chat_history_lines(history)) or "（无）",
        "search_terms_used": [query], "retrieval_terms_used": [query], "alias_text": "", "relation_text": "",
        "alias_hints": [], "relation_hints": [], "query_resolution_text": "",
        "summary": {"subject_count": 1, "evidence_count": 1, "gaokao_hit_count": 0, "coverage_line": "覆盖 1 个学科"},
        "suggested_questions": ["追问一"], "phase": phase,
    }


class MainIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.context_calls = 0
        self.answer = "加速度与合力成正比，见 [物理·必修第一册·p92]；另见 [物理·必修第一册·p1234]。"

        async def upstream(prompt):
            self.calls.append(prompt)
            return self.answer

        def counted_context(*args, **kwargs):
            self.context_calls += 1
            return fake_chat_context(*args, **kwargs)

        self.guard = main.AI_GUARD
        self._saved = (self.guard._upstream, main._build_chat_context_for_request, main._build_dict_chat_context_for_request,
                       main._ai_gaokao_context_for_request)
        self.guard._upstream = upstream
        main._build_chat_context_for_request = counted_context
        main._build_dict_chat_context_for_request = lambda headword, phase=None: {
            "dict_context": "[辞源·p12] 之：往也。", "textbook_context": "[教材·必修上·p30] 之", "gaokao_context": "",
            "summary": {"evidence_count": 2},
        }
        main._ai_gaokao_context_for_request = lambda question_id: {
            "question_title": "2024 全国卷", "question_subject": "物理", "question_year": 2024, "question_category": "全国甲卷",
            "question_type": "objective", "matched_concepts": [{"concept": "牛顿第二定律"}], "precomputed_analysis": None,
            "links": [{"id": 1, "subject": "物理", "title": "必修第一册", "book_key": "physics_b1", "section": 87, "summary": "牛顿第二定律", "text": "T" * 2000}],
            "cross_links": [], "_question_text": "一物体在水平面上……",
        }
        self.guard._cache.clear()
        self.guard._status_budget = None
        self.budget_before = self.guard.budget.snapshot(main.AI_GUARD_CONFIG.daily_budget)["count"]
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app, client=("127.0.0.1", 40000)), base_url=ORIGIN)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.guard.drain_logs()
        (self.guard._upstream, main._build_chat_context_for_request, main._build_dict_chat_context_for_request,
         main._ai_gaokao_context_for_request) = self._saved

    async def post(self, path, payload):
        return await self.client.post(path, headers=HEADERS, content=json.dumps(payload, ensure_ascii=False).encode())

    def log_rows(self):
        con = sqlite3.connect(main.DB_PATH)
        con.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in con.execute("SELECT * FROM ai_chat_logs ORDER BY id")]
        finally:
            con.close()

    async def test_real_app_flow(self):
        # 1. No token → 401, nothing runs.
        refused = await self.post("/api/chat", {"query": "牛顿第二定律", "user_message": "请解释"})
        self.assertEqual((refused.status_code, refused.json()["code"]), (401, "AI_SESSION_REQUIRED"))
        self.assertEqual((self.calls, self.context_calls), ([], 0))

        # 2. Admission, then one question = one retrieval + one upstream call.
        session = await self.client.post("/api/ai/session", headers=HEADERS, content=b"{}")
        self.assertEqual(session.status_code, 200)
        history = [{"role": "user", "content": "上一问"}, {"role": "assistant", "content": "上一答"}]
        payload = {"query": "牛顿第二定律", "user_message": "请解释其核心", "history": history, "phase": "高中"}
        answer = await self.post("/api/chat", payload)
        self.assertEqual(answer.status_code, 200, answer.text)
        data = answer.json()
        self.assertEqual((len(self.calls), self.context_calls), (1, 1))
        self.assertEqual(data["answer"], self.answer)
        self.assertEqual(data["provider"], main.AI_SERVICE_LABEL)
        evidence = data["context"]["evidence"]
        self.assertEqual((evidence[0]["book_key"], evidence[0]["section"], evidence[0]["logical_page"]), ("physics_b1", 87, 92))
        self.assertNotIn("text", evidence[0])
        self.assertEqual(data["citation_check"]["unverified"], ["[物理·必修第一册·p1234]"])
        self.assertEqual(data["context"]["suggested_questions"], ["追问一"])
        prompt = self.calls[0]
        self.assertIn("请解释其核心", prompt)
        self.assertIn("用户: 上一问", prompt)
        self.assertIn("[物理·必修第一册·p92]", prompt)
        self.assertLessEqual(len(prompt), main.AI_GUARD_CONFIG.max_prompt_chars)

        # 3. Identical question again → cached, still one upstream call.
        again = await self.post("/api/chat", payload)
        self.assertTrue(again.json()["cached"])
        self.assertEqual((len(self.calls), self.context_calls), (1, 1))

        # 4. Dictionary and gaokao go through the same guard; client-authored context is ignored.
        dict_answer = await self.post("/api/dict/chat", {"headword": "之", "user_message": "讲讲", "dict_context": "IGNORE-ME " * 50})
        self.assertEqual(dict_answer.status_code, 200, dict_answer.text)
        self.assertNotIn("IGNORE-ME", self.calls[1])
        self.assertIn("[辞源·p12]", self.calls[1])
        gaokao = await self.post("/api/gaokao/chat", {"question_id": 42, "phase": "高中"})
        self.assertEqual(gaokao.status_code, 200, gaokao.text)
        self.assertEqual(gaokao.json()["question"], {"subject": "物理", "year": 2024, "category": "全国甲卷", "link_count": 1, "cross_link_count": 0})
        self.assertEqual(gaokao.json()["sources"][0]["book_key"], "physics_b1")
        self.assertIn("【题干】\n一物体在水平面上", self.calls[2])
        self.assertEqual(len(self.calls), 3)

        # 5. Retired endpoints.
        self.assertEqual((await self.post("/api/chat/log", {"query": "伪造", "user_message": "伪造"})).status_code, 410)
        retired_context = await self.post("/api/chat/context", payload)   # no retrieval; fixed reply for old pages
        self.assertEqual((retired_context.status_code, retired_context.json()), (200, LEGACY["context"]["body"]))
        self.assertEqual(self.context_calls, 1)

        # 6. Server-side log in the real table: additive columns, no raw text.
        await self.guard.drain_logs()
        rows = self.log_rows()
        self.assertEqual([row["kind"] for row in rows], ["chat", "dict", "gaokao"])
        for row in rows:
            self.assertEqual((row["query"], row["user_message"], row["success"], row["outcome"]), ("", "", 1, "ok"))
        blob = json.dumps(rows, ensure_ascii=False)
        for forbidden in ("牛顿第二定律", "请解释其核心", "伪造", "127.0.0.1", "IGNORE-ME"):
            self.assertNotIn(forbidden, blob)
        self.assertEqual(rows[0]["query_len"], len("牛顿第二定律"))
        self.assertEqual(rows[0]["evidence_count"], 1)

        # 7. Budget (5) is a hard stop across all three endpoints; the counter file lives in STATE_ROOT.
        for index in range(2):
            self.assertEqual((await self.post("/api/chat", dict(payload, query=f"新问题{index}"))).status_code, 200)
        stopped = await self.post("/api/chat", dict(payload, query="第六个问题"))
        self.assertEqual((stopped.status_code, stopped.json()["code"]), (503, "AI_BUDGET_EXHAUSTED"))
        self.assertEqual(len(self.calls), 5)
        status = (await self.client.get("/api/ai/status")).json()
        self.assertEqual((status["budget"]["count"], status["budget"]["limit"], status["budget"]["remaining"]), (5, 5, 0))
        self.assertTrue((Path(os.environ["STATE_ROOT"]) / "ai_guard" / "ai_budget.sqlite3").exists())

    async def test_old_client_on_the_real_app(self):
        """A page still running the 035d2d4 script (no client marker) against the real app."""
        chat = await self.client.post("/api/chat", headers=OLD_CLIENT_HEADERS, content=json.dumps({"query": "牛顿第二定律", "user_message": "x", "history": []}).encode())
        self.assertEqual((chat.status_code, chat.json()), (200, LEGACY["chat"]["body"]))
        context = await self.client.post("/api/chat/context", headers=OLD_CLIENT_HEADERS, content=b"{}")
        self.assertEqual((context.status_code, context.json()), (200, LEGACY["context"]["body"]))
        dict_chat = await self.client.post("/api/dict/chat", headers=OLD_CLIENT_HEADERS, content=b'{"headword":"x","user_message":"y"}')
        self.assertEqual((dict_chat.status_code, dict_chat.json()), (200, LEGACY["dict"]["body"]))
        link = await self.client.get("/api/gaokao/link?question_id=12&limit=5", headers={"Sec-Fetch-Site": "same-origin", "Referer": ORIGIN + "/"})
        self.assertEqual((link.status_code, link.json()), (410, LEGACY["gaokao_link"]["body"]))
        self.assertEqual((self.calls, self.context_calls), ([], 0))
        self.assertEqual(self.guard.status()["budget"]["count"], self.budget_before)

    async def test_no_model_was_loaded(self):
        self.assertFalse(main.FAISS_AVAILABLE)
        for name in ("faiss_index", "supplemental_faiss_index", "embedder", "reranker"):
            self.assertIsNone(getattr(main, name), name)

    async def test_served_pages_and_assets(self):
        for page, script in (("/", "assets/app.js"), ("/chuzhong.html", "assets/app.js"), ("/dict.html", "assets/dict.js"), ("/chuzhong-dict.html", "assets/dict.js")):
            html = (await self.client.get(page)).text
            self.assertLess(html.index("assets/ai-client.js"), html.index(script))
        client_js = await self.client.get("/assets/ai-client.js")
        self.assertEqual(client_js.status_code, 200)
        self.assertIn("window.SunAI", client_js.text)

    async def test_upstream_headers_and_single_attempt(self):
        """The real _call_ai_service, pointed at a mock transport: one POST, server-side credentials only."""
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"answer": "好"})

        real_post = main.ai_guard.post_upstream

        async def patched(url, payload, headers, **kwargs):
            return await real_post(url, payload, headers, transport=httpx.MockTransport(handler), **kwargs)

        main.ai_guard.post_upstream = patched
        try:
            self.assertEqual(await main._call_ai_service("PROMPT"), "好")
        finally:
            main.ai_guard.post_upstream = real_post
        self.assertEqual(len(seen), 1)
        body = json.loads(seen[0].content)
        self.assertEqual(body["prompt"], "PROMPT")
        self.assertEqual(seen[0].headers["origin"], main.AI_SERVICE_ORIGIN)
        self.assertEqual((main.AI_GUARD_CONFIG.retries, main.AI_SERVICE_RETRIES), (0, 0))
        self.assertLessEqual(main.AI_GUARD_CONFIG.upstream_deadline_sec, 30.0)


sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_test_timeout  # noqa: E402

ai_test_timeout.protect(globals())

if __name__ == "__main__":
    unittest.main()
