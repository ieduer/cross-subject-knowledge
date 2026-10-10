"""Grep-style assertions over the served frontend and over main.py's source.

main.py cannot be imported in the test venv (torch / FAISS / the runtime DB are
not installed), so its integration with ai_guard is asserted on the source text.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "frontend" / "assets"
JS_FILES = sorted(ASSETS.glob("*.js"))
HTML_FILES = sorted((ROOT / "frontend").glob("*.html"))
MAIN = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def strip_js_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return "\n".join(line for line in source.splitlines() if not line.strip().startswith("//"))


def function_body(source: str, header: str) -> str:
    """Text of a top-level JS function, from its header to the next top-level declaration."""
    start = source.index(header)
    match = re.search(r"\n(?:async function |function |const |let |el\.|document\.|window\.)", source[start + len(header):])
    end = start + len(header) + (match.start() if match else len(source))
    return source[start:end]


class FrontendStaticTests(unittest.TestCase):
    def test_expected_files_exist(self):
        self.assertEqual([p.name for p in JS_FILES], ["ai-client.js", "app.js", "dict.js"])

    def test_no_model_host_anywhere_in_served_js_or_html(self):
        forbidden = re.compile(
            r"ai\.bdfz\.net|apis\.bdfz|workers\.dev|generativelanguage\.googleapis|aiplatform\.googleapis|openai|anthropic|gemini-[a-z0-9]",
            re.IGNORECASE,
        )
        for path in JS_FILES + HTML_FILES:
            hits = [line.strip()[:120] for line in read(path).splitlines() if forbidden.search(line)]
            self.assertEqual(hits, [], path.name)

    def test_every_absolute_url_in_js_is_a_known_static_host(self):
        allowed = ("https://img.rdfzer.com", "http://www.w3.org/")
        for path in JS_FILES:
            for url in re.findall(r"https?://[^\s'\"`)<>]+", read(path)):
                self.assertTrue(url.startswith(allowed) or "github.com" in url or "bdfz.net" in url and "ai." not in url, f"{path.name}: {url}")

    def test_no_race_no_browser_fallback_no_auto_trigger(self):
        banned = (
            "Promise.any", "Promise.race", "browserFallback", "requestDirectChat", "buildClientAIPrompt",
            "buildFallbackContextPayload", "maybeAutoTriggerAI", "aiAutoRequestedKey", "AI_AUTO_TRIGGER",
            "AI_BROWSER_FALLBACK", "AI_API", "/api/chat/context", "/api/chat/log", "logClientAIChat",
            "fetchAIContext", "requestServerChat",
        )
        for path in JS_FILES:
            source = read(path)
            for token in banned:
                self.assertNotIn(token, source, f"{path.name}: {token}")

    def test_ai_endpoints_are_reached_only_through_the_guarded_client(self):
        app = strip_js_comments(read(ASSETS / "app.js"))
        dict_js = strip_js_comments(read(ASSETS / "dict.js"))
        self.assertEqual(re.findall(r"[^\n]*/api/chat\b[^\n]*", app), ["    const request = window.SunAI.ask(`${API}/api/chat`, buildAIServerPayload(cleanMessage, history));"])
        self.assertEqual(len(re.findall(r"/api/gaokao/chat", app)), 1)
        self.assertIn("window.SunAI.ask(`${API}/api/gaokao/chat`", app)
        self.assertEqual(len(re.findall(r"/api/dict/chat", dict_js)), 1)
        self.assertIn("window.SunAI.ask(`${API}/api/dict/chat`", dict_js)
        self.assertEqual(app.count("SunAI.ask("), 2)
        self.assertEqual(dict_js.count("SunAI.ask("), 1)
        for source in (app, dict_js):
            self.assertNotRegex(source, r"fetch(Json)?\([^)]*api/(dict/|gaokao/)?chat")
            self.assertNotIn("/api/ai/session", source)

    def test_client_module_is_single_shot_and_cancellable(self):
        client = strip_js_comments(read(ASSETS / "ai-client.js"))
        self.assertEqual(client.count("fetch("), 2, "one admission call + one AI call")
        self.assertIn("fetch(SESSION_PATH", client)
        self.assertIn("AbortController", client)
        self.assertIn("signal: controller.signal", client)
        self.assertIn("credentials: 'same-origin'", client)
        for banned in ("setInterval", "while (", "for (let attempt", "retries", "XMLHttpRequest", "sendBeacon", "http://", "https://"):
            self.assertNotIn(banned, client, banned)
        self.assertRegex(client, r"\.slice\(-limits\.max_history_items\)")
        self.assertRegex(client, r"\.slice\(0, limits\.max_history_item_chars\)")

    def test_main_page_call_sites_are_explicit_user_actions(self):
        app = read(ASSETS / "app.js")
        allowed = {
            "sendAIMessage(": {
                "btn.addEventListener('click', () => sendAIMessage(btn.dataset.prompt));",
                "sendAIMessage(message);",
                "async function sendAIMessage(userMessage) {",
                "await sendAIMessage(aiFollowupInput.value);",
            },
            "requestAISynthesis(": {
                "if (aiBtn) aiBtn.addEventListener('click', () => requestAISynthesis());",
                "async function requestAISynthesis() {",
            },
            "requestAIFollowup(": {
                "if (aiSendBtn) aiSendBtn.addEventListener('click', () => requestAIFollowup());",
                "requestAIFollowup();",
                "async function requestAIFollowup() {",
            },
            "retryLastAIMessage(": {
                "if (retryBtn) retryBtn.addEventListener('click', () => retryLastAIMessage());",
                "function retryLastAIMessage() {",
            },
        }
        for token, expected in allowed.items():
            for line in (line.strip() for line in app.splitlines() if token in line):
                if token == "sendAIMessage(" and line.startswith("await sendAIMessage(`"):
                    continue  # the three canned prompts inside requestAISynthesis
                self.assertIn(line, expected, f"unexpected call site: {line}")
        synthesis = function_body(app, "async function requestAISynthesis() {")
        self.assertEqual(synthesis.count("await sendAIMessage(`"), 3)
        self.assertEqual(app.count("await sendAIMessage(`"), 3)
        # The Enter-key call sits inside the textarea keydown listener.
        self.assertRegex(app, r"aiFollowupInput\.addEventListener\('keydown', \(e\) => \{\s+if \(e\.key === 'Enter' && !e\.shiftKey\) \{\s+e\.preventDefault\(\);\s+requestAIFollowup\(\);")
        # Nothing in the search / render / subject-switch path can start AI.
        for header in ("async function doSearch(", "function resetAIConversation() {", "function renderAIStarters() {"):
            body = function_body(app, header).replace(
                "btn.addEventListener('click', () => sendAIMessage(btn.dataset.prompt));", "")
            for token in ("sendAIMessage(", "requestAISynthesis(", "SunAI.ask(", "requestGaokaoAI("):
                self.assertNotIn(token, body, f"{header} must not trigger AI")
        self.assertNotRegex(app, r"setTimeout\([^)]*(sendAIMessage|requestAISynthesis|requestGaokaoAI)")
        self.assertIn("让 AI 结合检索结果解答", app)

    def test_main_page_single_flight_cancel_and_stale_guard(self):
        app = read(ASSETS / "app.js")
        send = function_body(app, "async function sendAIMessage(userMessage) {")
        self.assertIn("aiRequestPending", send.split("\n")[1], "second send is refused while one is pending")
        self.assertIn("window.SunAI.boundedHistory(aiConversation)", send)
        self.assertEqual(send.count("SunAI.ask("), 1)
        self.assertIn("if (epoch !== aiConversationEpoch) return;", send)
        self.assertIn("executionKey !== getAIExecutionKey()", send)
        self.assertNotIn("catch(() =>", send, "no fallback chained onto the request")
        reset = function_body(app, "function resetAIConversation() {")
        self.assertIn("aiConversationEpoch += 1;", reset)
        self.assertIn("aiActiveRequest.cancel();", reset)
        self.assertIn("ai-cancel-btn", app)
        self.assertIn("ai-retry-btn", app)
        busy = function_body(app, "function setAIBusy(isBusy) {")
        self.assertIn("aiSendBtn.disabled = isBusy", busy)

    def test_model_text_is_never_turned_into_a_link(self):
        app = read(ASSETS / "app.js")
        render = function_body(app, "function renderAIConversation() {")
        self.assertIn('<div class="ai-msg-text">${escHtml(msg.content)}</div>', render)
        sources = function_body(app, "function renderAISources(sources) {")
        self.assertIn('data-book-key="${escAttr(src.book_key', sources)
        self.assertIn("src.section", sources)
        self.assertIn("sources: (contextPayload.evidence || [])", app, "chips come from server retrieval records")
        self.assertIn('<div class="gk-ai-text">${escHtml(String(data.answer', app)
        dict_js = read(ASSETS / "dict.js")
        self.assertIn('<div class="dict-chat-bubble">${escHtml(item.content)}${action}</div>', dict_js)
        for source in (app, dict_js):
            self.assertNotRegex(source, r"innerHTML\s*=\s*(data\.answer|msg\.content|item\.content)")

    def test_dictionary_page_never_calls_ai_on_lookup_or_page_load(self):
        dict_js = read(ASSETS / "dict.js")
        run_search = function_body(dict_js, "async function runSearch(initialQuery) {")
        for token in ("sendChatMessage(", "SunAI", "/api/dict/chat"):
            self.assertNotIn(token, run_search, "lookup (incl. ?q= load) must not call AI")
        self.assertIn("state.chatOfferReady = true;", run_search)
        call_lines = sorted(line.strip() for line in dict_js.splitlines() if "sendChatMessage(" in line)
        self.assertEqual(call_lines, sorted([
            "sendChatMessage(buildChatOfferPrompt(state.query), { silentUser: true });",
            "sendChatMessage(message, { silentUser });",
            "async function sendChatMessage(message, { silentUser = false, queryKey = state.queryKey, expectedQuery = state.query } = {}) {",
            "sendChatMessage(message);",
        ]))
        actions = function_body(dict_js, "function bindChatActions() {")
        self.assertIn("button.addEventListener('click'", actions)
        self.assertIn("让 AI 结合检索结果解答", dict_js)
        send = function_body(dict_js, "async function sendChatMessage(message,")
        self.assertIn("state.chatInFlight", send.split("\n")[2])
        self.assertIn("window.SunAI.boundedHistory(state.chatHistory)", send)
        self.assertIn("if (queryKey !== state.queryKey || expectedQuery !== state.query) return;", send)
        for token in ("dict_context", "textbook_context", "gaokao_context"):
            self.assertNotIn(token, send, "client-authored context is no longer uploaded")
        reset = function_body(dict_js, "function resetPanelsForSearch() {") if "function resetPanelsForSearch() {" in dict_js else dict_js
        self.assertIn("state.chatRequest.cancel();", reset)
        self.assertIn('data-chat-action="cancel"', dict_js)
        self.assertIn('data-chat-action="retry"', dict_js)

    def test_pages_load_the_client_before_their_script(self):
        for page in HTML_FILES:
            html = read(page)
            main_script = "assets/dict.js" if "dict" in page.name else "assets/app.js"
            self.assertIn("assets/ai-client.js?v=", html, page.name)
            self.assertLess(html.index("assets/ai-client.js"), html.index(main_script), page.name)
            self.assertNotIn("?v=20260507a", html.split("assets/ai-client.js")[1], f"{page.name}: stale cache key keeps the old auto-calling script")


class MainSourceTests(unittest.TestCase):
    def test_ai_routes_are_registered_only_by_the_guard(self):
        for route in ("/api/chat", "/api/chat/context", "/api/chat/log", "/api/dict/chat", "/api/gaokao/chat", "/api/ai/"):
            self.assertNotRegex(MAIN, r'@app\.(post|get|put|api_route)\(\s*"' + re.escape(route), route)
        self.assertEqual(MAIN.count("app.include_router("), 1)
        self.assertIn("ai_guard.build_router(", MAIN)
        self.assertRegex(MAIN, r'\{"chat": _ai_build_chat, "dict": _ai_build_dict_chat, "gaokao": _ai_build_gaokao_chat\}')
        self.assertLess(MAIN.index("app.include_router("), MAIN.index('app.mount("/assets"'))
        self.assertNotIn("Body(...)", MAIN, "no handler accepts an uncapped JSON body")

    def test_single_upstream_path_through_the_guard(self):
        self.assertEqual(len(re.findall(r"_call_ai_service\(", MAIN)), 1, "defined once, never called directly")
        self.assertEqual(MAIN.count("upstream=_call_ai_service"), 1)
        self.assertEqual(MAIN.count("ai_guard.Guard("), 1)
        self.assertEqual(MAIN.count("ai_guard.post_upstream("), 1)
        for banned in ("httpx.AsyncClient", "client.post(", "httpx.post(", "requests.post(", "urllib.request.urlopen(", "asyncio.sleep(AI_SERVICE_RETRY_DELAY)"):
            self.assertNotIn(banned, MAIN, banned)
        self.assertNotRegex(MAIN, r"for attempt in range\(AI_SERVICE_RETRIES")

    def test_safe_defaults(self):
        self.assertIn('AI_SERVICE_RETRIES = min(1, max(0, int(os.getenv("AI_SERVICE_RETRIES", "0"))))', MAIN)
        self.assertIn('AI_SERVICE_TIMEOUT = min(30.0, float(os.getenv("AI_SERVICE_TIMEOUT_SEC", "30")))', MAIN)
        self.assertIn("AI_GUARD_CONFIG = ai_guard.GuardConfig.from_env(state_root=STATE_ROOT)", MAIN)
        self.assertNotRegex(MAIN, r'os\.getenv\("AI_ENABLED"')  # enablement is decided in ai_guard only
        for secret_env in ("AI_SESSION_KEY", "AI_INTERNAL_TOKEN"):
            for line in MAIN.splitlines():
                if secret_env in line:
                    self.assertNotRegex(line, r"print\(|logger\.|return ", line)

    def test_logging_is_server_side_and_minimised(self):
        self.assertNotIn("INSERT INTO ai_chat_logs", MAIN, "only ai_guard.write_chat_log writes this table")
        self.assertIn("ai_guard.write_chat_log(con, record, provider=AI_SERVICE_LABEL)", MAIN)
        self.assertIn("ai_guard.ensure_chat_log_columns(con)", MAIN)
        self.assertEqual(MAIN.count("log=_write_ai_chat_log_record"), 1)
        for gone in ("def chat_log(", "def log_ai_chat(", "def _write_ai_chat_log("):
            self.assertNotIn(gone, MAIN)
        for destructive in ("DROP TABLE ai_chat_logs", "DROP COLUMN", "DELETE FROM ai_chat_logs"):
            self.assertNotIn(destructive, MAIN)
        self.assertNotRegex(MAIN, r"FROM ai_chat_logs", "nothing in the app reads the table, so raw text is not needed")

    def test_builders_return_retrieval_based_citations(self):
        chat = MAIN[MAIN.index("async def _ai_build_chat("):MAIN.index("# ── Dictionary APIs")]
        self.assertIn('evidence = ai_guard.sanitize_evidence(context_payload.get("evidence"))', chat)
        self.assertIn('"citation_check": ai_guard.check_citations(answer, evidence)', chat)
        self.assertEqual(chat.count("_build_chat_context_for_request"), 1, "one retrieval per question")
        gaokao = MAIN[MAIN.index("async def _ai_build_gaokao_chat("):MAIN.index("AI_GUARD = ai_guard.Guard(")]
        self.assertIn("ai_guard.sanitize_evidence(links[:3] + cross_links[:2], limit=5)", gaokao)
        self.assertNotIn('fields["prompt"]', MAIN)
        self.assertNotRegex(MAIN, r'fields(\.get)?\(?\[?"(dict_context|textbook_context|gaokao_context)"')

    def test_guard_module_keeps_fastapi_annotations_resolvable(self):
        guard_source = (ROOT / "backend" / "ai_guard.py").read_text(encoding="utf-8")
        self.assertNotRegex(guard_source, r"(?m)^from __future__ import annotations")
        self.assertNotRegex(guard_source, r"(?m)^(import|from) (torch|faiss|sentence_transformers|main)\b")

    def test_deploy_warmup_no_longer_uses_a_chat_route(self):
        deploy = (ROOT / "scripts" / "deploy_vps.sh").read_text(encoding="utf-8")
        self.assertNotRegex(deploy, r"(?m)^[^#\n]*/api/chat")


if __name__ == "__main__":
    unittest.main()
