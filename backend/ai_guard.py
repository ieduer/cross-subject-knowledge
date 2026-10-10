"""AI cost / abuse guard for the sun.bdfz.net conversation endpoints.

This module is deliberately importable without ``main.py`` (no torch, no
FAISS, no database): the core only needs the standard library, ``httpx`` is
imported when an upstream call is made and ``fastapi`` when the router is
built. ``main.py`` supplies the retrieval / prompt builders; everything that
decides whether a paid model call may happen lives here.

See ``docs/AI_GUARD.md`` for the request flow, every control and its env var.
"""
import asyncio
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional

# Hard ceilings that no env value can raise.
HARD_MAX_UPSTREAM_DEADLINE_SEC = 30.0
HARD_MAX_RETRIES = 1
MIN_SESSION_KEY_CHARS = 32
SESSION_COOKIE = "sun_ai_session"
ANSWER_TRUNCATION_MARK = "\n……（回答过长，已截断）"

_USER_MESSAGES = {
    "AI_DISABLED": "AI 解读当前未开启。",
    "AI_BUDGET_EXHAUSTED": "今日 AI 解读额度已用完，将在 UTC 0 点（北京时间 8 点）恢复。",
    "AI_BUDGET_UNAVAILABLE": "AI 额度计数暂不可用，已暂停 AI 解读。",
    "AI_SUBJECT_BUDGET_EXHAUSTED": "当前网络今日的 AI 解读次数已用完，明日恢复。",
    "AI_BUSY": "AI 解读排队已满，请稍后再试。",
    "AI_SESSION_REQUIRED": "AI 会话已过期，请重试。",
    "forbidden_origin": "请求来源不被允许。",
    "rate_limited": "请求过于频繁，请稍后再试。",
    "length_required": "请求缺少长度声明。",
    "body_too_large": "请求内容过长。",
    "unsupported_media_type": "请求格式不被支持。",
    "bad_json": "请求格式有误。",
    "bad_request": "请求参数有误。",
    "input_too_long": "输入内容过长，请缩短后再试。",
    "history_too_long": "对话历史过长，请开启新的对话。",
    "prompt_too_large": "本次检索证据过长，无法提交给 AI，请缩小检索范围。",
    "ai_timeout": "AI 服务响应超时，请稍后手动重试。",
    "ai_unreachable": "AI 服务暂时不可用，请稍后手动重试。",
    "ai_upstream": "AI 服务暂时不可用，请稍后手动重试。",
    "ai_oversize": "AI 服务返回内容异常，请稍后手动重试。",
    "ai_empty": "AI 服务没有返回内容，请稍后手动重试。",
    "cancelled": "请求已取消。",
    "not_found": "未找到对应内容。",
    "retired": "该接口已停用。",
    "internal_error": "AI 解读出错，请稍后再试。",
}


class AIGuardError(Exception):
    """A refusal or failure with a stable machine code and a safe user message."""

    def __init__(self, status: int, code: str, *, retry_after: Optional[float] = None, message: Optional[str] = None):
        super().__init__(code)
        self.status = int(status)
        self.code = code
        self.retry_after = None if retry_after is None else max(1, int(retry_after + 0.999))
        self.message = message or _USER_MESSAGES.get(code, _USER_MESSAGES["internal_error"])

    def body(self) -> dict:
        payload = {"detail": self.message, "code": self.code}
        if self.retry_after is not None:
            payload["retry_after"] = self.retry_after
        return payload


# ── Configuration ────────────────────────────────────────────────────────────

def _env_int(env: Mapping[str, str], name: str, default: int, *, lo: int = 0, hi: Optional[int] = None) -> int:
    raw = str(env.get(name, "")).strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    value = max(lo, value)
    return min(hi, value) if hi is not None else value


def _env_float(env: Mapping[str, str], name: str, default: float, *, lo: float = 0.0, hi: Optional[float] = None) -> float:
    raw = str(env.get(name, "")).strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = default
    value = max(lo, value)
    return min(hi, value) if hi is not None else value


def _parse_networks(raw: str) -> tuple:
    networks = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return tuple(networks)


@dataclass(frozen=True)
class GuardConfig:
    # Enablement: ALL three are required, otherwise every AI endpoint is 503.
    enabled_flag: bool = False
    session_key: bytes = b""
    daily_budget: int = 0
    # Optional per-subject daily cap (0 = off).
    subject_daily_cap: int = 0
    allowed_origins: tuple = ("https://sun.bdfz.net",)
    trusted_proxies: tuple = field(default_factory=lambda: _parse_networks("127.0.0.0/8,::1/128"))
    session_ttl_sec: int = 1800
    cookie_secure: bool = True
    # Request caps.
    max_body_bytes: int = 49152
    max_query_chars: int = 200
    max_message_chars: int = 800
    max_history_items: int = 6
    max_history_item_chars: int = 1200
    max_prompt_chars: int = 24000
    # Output caps.
    max_upstream_bytes: int = 262144
    max_answer_chars: int = 4000
    # Rate limits (token buckets, per subject).
    rate_burst: int = 5
    rate_per_minute: float = 10.0
    session_rate_burst: int = 10
    session_rate_per_minute: float = 30.0
    # Concurrency.
    max_concurrent: int = 2
    max_queue: int = 4
    queue_wait_sec: float = 15.0
    # Upstream.
    upstream_deadline_sec: float = HARD_MAX_UPSTREAM_DEADLINE_SEC
    retries: int = 0
    # Answer cache.
    cache_ttl_sec: int = 600
    cache_max_entries: int = 256
    cache_max_entry_bytes: int = 65536
    state_dir: Path = Path("state") / "ai_guard"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, *, state_root: Optional[Path] = None) -> "GuardConfig":
        env = os.environ if env is None else env
        key = str(env.get("AI_SESSION_KEY", "")).strip()
        origins = tuple(
            item.strip().rstrip("/")
            for item in str(env.get("AI_ALLOWED_ORIGINS", "https://sun.bdfz.net")).split(",")
            if item.strip()
        )
        root = Path(state_root) if state_root is not None else Path(str(env.get("STATE_ROOT", "state")))
        return cls(
            enabled_flag=str(env.get("AI_ENABLED", "")).strip() == "1",
            session_key=key.encode("utf-8") if len(key) >= MIN_SESSION_KEY_CHARS else b"",
            daily_budget=_env_int(env, "AI_DAILY_REQUEST_BUDGET", 0),
            subject_daily_cap=_env_int(env, "AI_SUBJECT_DAILY_CAP", 0),
            allowed_origins=origins,
            trusted_proxies=_parse_networks(str(env.get("AI_TRUSTED_PROXIES", "127.0.0.0/8,::1/128"))),
            session_ttl_sec=_env_int(env, "AI_SESSION_TTL_SEC", 1800, lo=60, hi=86400),
            cookie_secure=str(env.get("AI_COOKIE_SECURE", "1")).strip() != "0",
            max_body_bytes=_env_int(env, "AI_MAX_BODY_BYTES", 49152, lo=1024, hi=262144),
            max_query_chars=_env_int(env, "AI_MAX_QUERY_CHARS", 200, lo=1, hi=200),
            max_message_chars=_env_int(env, "AI_MAX_MESSAGE_CHARS", 800, lo=1, hi=4000),
            max_history_items=_env_int(env, "AI_MAX_HISTORY_ITEMS", 6, lo=0, hi=12),
            max_history_item_chars=_env_int(env, "AI_MAX_HISTORY_ITEM_CHARS", 1200, lo=1, hi=4000),
            max_prompt_chars=_env_int(env, "AI_MAX_PROMPT_CHARS", 24000, lo=1000, hi=60000),
            max_upstream_bytes=_env_int(env, "AI_MAX_UPSTREAM_BYTES", 262144, lo=1024, hi=1048576),
            max_answer_chars=_env_int(env, "AI_MAX_ANSWER_CHARS", 4000, lo=100, hi=20000),
            rate_burst=_env_int(env, "AI_RATE_BURST", 5, lo=1, hi=100),
            rate_per_minute=_env_float(env, "AI_RATE_PER_MINUTE", 10.0, lo=0.1, hi=600.0),
            session_rate_burst=_env_int(env, "AI_SESSION_RATE_BURST", 10, lo=1, hi=200),
            session_rate_per_minute=_env_float(env, "AI_SESSION_RATE_PER_MINUTE", 30.0, lo=0.1, hi=1200.0),
            max_concurrent=_env_int(env, "AI_MAX_CONCURRENT", 2, lo=1, hi=8),
            max_queue=_env_int(env, "AI_MAX_QUEUE", 4, lo=0, hi=32),
            queue_wait_sec=_env_float(env, "AI_QUEUE_WAIT_SEC", 15.0, lo=0.0, hi=60.0),
            upstream_deadline_sec=_env_float(
                env, "AI_SERVICE_TIMEOUT_SEC", HARD_MAX_UPSTREAM_DEADLINE_SEC, lo=1.0, hi=HARD_MAX_UPSTREAM_DEADLINE_SEC
            ),
            retries=_env_int(env, "AI_SERVICE_RETRIES", 0, lo=0, hi=HARD_MAX_RETRIES),
            cache_ttl_sec=_env_int(env, "AI_CACHE_TTL_SEC", 600, lo=0, hi=86400),
            cache_max_entries=_env_int(env, "AI_CACHE_MAX_ENTRIES", 256, lo=0, hi=4096),
            cache_max_entry_bytes=_env_int(env, "AI_CACHE_MAX_ENTRY_BYTES", 65536, lo=1024, hi=1048576),
            state_dir=root / "ai_guard",
        )

    def disabled_reasons(self) -> list:
        reasons = []
        if not self.enabled_flag:
            reasons.append("AI_ENABLED")
        if not self.session_key:
            reasons.append("AI_SESSION_KEY")
        if self.daily_budget <= 0:
            reasons.append("AI_DAILY_REQUEST_BUDGET")
        return reasons

    @property
    def enabled(self) -> bool:
        return not self.disabled_reasons()

    def public_limits(self) -> dict:
        return {
            "max_query_chars": self.max_query_chars,
            "max_message_chars": self.max_message_chars,
            "max_history_items": self.max_history_items,
            "max_history_item_chars": self.max_history_item_chars,
            "max_answer_chars": self.max_answer_chars,
        }


# ── Client subject ───────────────────────────────────────────────────────────

def _parse_ip(value: Optional[str]):
    try:
        return ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return None


def client_subject(peer_host: Optional[str], x_real_ip: Optional[str], trusted_proxies: tuple) -> str:
    """Coarse quota subject: IPv4 address or IPv6 /64.

    ``X-Real-IP`` is honoured only when the TCP peer itself is a trusted proxy
    (loopback by default); a direct connection can never choose its own subject.
    """
    peer = _parse_ip(peer_host)
    addr = peer
    if peer is not None and x_real_ip and any(peer in net for net in trusted_proxies):
        forwarded = _parse_ip(x_real_ip)
        if forwarded is not None:
            addr = forwarded
    if addr is None:
        return "unknown"
    if addr.version == 6:
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network((int(addr) >> 64 << 64, 64)))
    return str(addr)


# ── Text normalisation / validation ──────────────────────────────────────────

_WS_RE = re.compile(r"\s+")


def normalize_text(value: Any) -> str:
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", str(value))).strip()


def dedup_key(kind: str, fields: Mapping[str, Any]) -> str:
    def norm(value):
        if isinstance(value, str):
            return normalize_text(value)
        if isinstance(value, (list, tuple)):
            return [norm(item) for item in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in sorted(value.items())}
        return value

    blob = json.dumps([kind, norm(dict(fields))], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _required_text(payload: Mapping[str, Any], name: str, limit: int) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise AIGuardError(400, "bad_request")
    value = value.strip()
    if not value:
        raise AIGuardError(400, "bad_request")
    if len(value) > limit:
        raise AIGuardError(413, "input_too_long")
    return value


def _optional_text(payload: Mapping[str, Any], name: str, limit: int) -> Optional[str]:
    value = payload.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise AIGuardError(400, "bad_request")
    value = value.strip()
    if len(value) > limit:
        raise AIGuardError(413, "input_too_long")
    return value or None


def _phase(payload: Mapping[str, Any]) -> Optional[str]:
    value = _optional_text(payload, "phase", 8)
    if value is not None and value not in ("高中", "初中"):
        raise AIGuardError(400, "bad_request")
    return value


def validate_history(raw: Any, cfg: GuardConfig) -> list:
    """Rejects (never truncates) history that exceeds the count or per-item cap."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AIGuardError(400, "bad_request")
    if len(raw) > cfg.max_history_items:
        raise AIGuardError(413, "history_too_long")
    history = []
    for item in raw:
        if not isinstance(item, dict):
            raise AIGuardError(400, "bad_request")
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            raise AIGuardError(400, "bad_request")
        content = content.strip()
        if len(content) > cfg.max_history_item_chars:
            raise AIGuardError(413, "history_too_long")
        if content:
            history.append({"role": role, "content": content})
    return history


def validate_chat(payload: Any, cfg: GuardConfig) -> dict:
    if not isinstance(payload, dict):
        raise AIGuardError(400, "bad_request")
    return {
        "query": _required_text(payload, "query", cfg.max_query_chars),
        "user_message": _required_text(payload, "user_message", cfg.max_message_chars),
        "history": validate_history(payload.get("history"), cfg),
        "scope_subject": _optional_text(payload, "scope_subject", 40),
        "book_key": _optional_text(payload, "book_key", 200),
        "phase": _phase(payload),
    }


def validate_dict_chat(payload: Any, cfg: GuardConfig) -> dict:
    if not isinstance(payload, dict):
        raise AIGuardError(400, "bad_request")
    # dict_context / textbook_context / gaokao_context from old clients are ignored:
    # the server always rebuilds its own evidence.
    return {
        "headword": _required_text(payload, "headword", min(40, cfg.max_query_chars)),
        "user_message": _required_text(payload, "user_message", cfg.max_message_chars),
        "history": validate_history(payload.get("history"), cfg),
        "phase": _phase(payload),
    }


def validate_gaokao_chat(payload: Any, cfg: GuardConfig) -> dict:
    if not isinstance(payload, dict):
        raise AIGuardError(400, "bad_request")
    question_id = payload.get("question_id")
    if isinstance(question_id, bool) or not isinstance(question_id, int) or not (0 < question_id < 2**53):
        raise AIGuardError(400, "bad_request")
    return {"question_id": question_id, "phase": _phase(payload)}


# ── Citations: only retrieval records may become links ───────────────────────

EVIDENCE_FIELDS = (
    "id", "subject", "title", "base_title", "display_title", "edition", "book_key", "section",
    "logical_page", "page_num", "page_url", "citation", "snippet", "source", "year", "category",
    "matched_term", "match_channel", "retrieval_source",
)
_CITATION_RE = re.compile(r"\[([^\[\]\n]{1,120}?)[·・]\s*(?:p|P|§|第)\s*(\d{1,5})\s*页?\]")


def sanitize_evidence(evidence: Any, *, limit: int = 8, snippet_chars: int = 600) -> list:
    """Whitelisted copy of retrieval records. Page / book references shown to the
    user are taken from these DB-provided fields and from nothing else."""
    clean = []
    for item in (evidence or [])[:limit]:
        if not isinstance(item, dict):
            continue
        row = {key: item[key] for key in EVIDENCE_FIELDS if key in item and item[key] is not None}
        if isinstance(row.get("snippet"), str):
            row["snippet"] = row["snippet"][:snippet_chars]
        clean.append(row)
    return clean


def check_citations(answer: str, evidence: list) -> dict:
    """Compare page references written by the model with the evidence list.

    The answer is always rendered as plain text; this only reports references
    that cannot be matched so the UI can say so. Nothing here creates a link.
    """
    known_pages = set()
    for item in evidence or []:
        for key in ("logical_page", "section", "page_num"):
            value = item.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
                known_pages.add((normalize_text(item.get("title") or ""), int(value)))
                known_pages.add((normalize_text(item.get("subject") or ""), int(value)))
    found = 0
    unverified = []
    for match in _CITATION_RE.finditer(answer or ""):
        found += 1
        label = normalize_text(match.group(1))
        page = int(match.group(2))
        ok = any(page == known_page and name and name in label for name, known_page in known_pages)
        if not ok:
            unverified.append(match.group(0)[:140])
    return {"found": found, "unverified_count": len(unverified), "unverified": unverified[:8]}


# ── Session admission ────────────────────────────────────────────────────────

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def issue_session_token(key: bytes, subject: str, ttl_sec: int, *, now: Optional[float] = None) -> tuple:
    if not key:
        raise AIGuardError(503, "AI_DISABLED")
    expires = int((time.time() if now is None else now) + ttl_sec)
    nonce = secrets.token_urlsafe(9)
    mac = hmac.new(key, f"v1|{expires}|{nonce}|{subject}".encode("utf-8"), hashlib.sha256).digest()
    return f"v1.{expires}.{nonce}.{_b64(mac)}", expires


def verify_session_token(key: bytes, token: Optional[str], subject: str, *, now: Optional[float] = None) -> bool:
    if not key or not token or len(token) > 256:
        return False
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != "v1" or not parts[1].isdigit():
        return False
    expires = int(parts[1])
    if expires <= (time.time() if now is None else now):
        return False
    expected = hmac.new(key, f"v1|{expires}|{parts[2]}|{subject}".encode("utf-8"), hashlib.sha256).digest()
    return hmac.compare_digest(_b64(expected), parts[3])


# ── Rate limiter ─────────────────────────────────────────────────────────────

class RateLimiter:
    """Per-subject token bucket with a bounded subject table."""

    def __init__(self, burst: int, per_minute: float, *, max_subjects: int = 20000, clock: Callable[[], float] = time.monotonic):
        self.burst = float(max(1, burst))
        self.rate = max(per_minute, 0.001) / 60.0
        self.max_subjects = max_subjects
        self.clock = clock
        self._buckets: "OrderedDict[str, tuple]" = OrderedDict()

    def take(self, subject: str) -> float:
        """Returns 0.0 when admitted, otherwise seconds until the next token."""
        now = self.clock()
        tokens, last = self._buckets.pop(subject, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if tokens >= 1.0:
            self._buckets[subject] = (tokens - 1.0, now)
            wait = 0.0
        else:
            self._buckets[subject] = (tokens, now)
            wait = (1.0 - tokens) / self.rate
        while len(self._buckets) > self.max_subjects:
            self._buckets.popitem(last=False)
        return wait


# ── Persistent daily budget ──────────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BudgetStore:
    """Hard daily stop. SQLite, keyed by UTC date, incremented atomically
    BEFORE the upstream call. Any storage error fails closed."""

    def __init__(self, path: Path, *, utcnow: Callable[[], datetime] = _utc_now):
        self.path = Path(path)
        self.utcnow = utcnow
        self._lock = threading.Lock()
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        if not self._ready:
            con.execute("CREATE TABLE IF NOT EXISTS ai_daily_budget (day TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0)")
            con.execute(
                "CREATE TABLE IF NOT EXISTS ai_subject_daily ("
                "day TEXT NOT NULL, subject TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, subject))"
            )
            self._ready = True
        return con

    def _day(self) -> str:
        return self.utcnow().astimezone(timezone.utc).strftime("%Y-%m-%d")

    def consume(self, limit: int, *, subject_hash: Optional[str] = None, subject_cap: int = 0) -> int:
        """Reserve one upstream call. Raises when the global or subject cap is reached."""
        if limit <= 0:
            raise AIGuardError(503, "AI_DISABLED")
        day = self._day()
        with self._lock:
            try:
                con = self._connect()
            except (sqlite3.Error, OSError) as exc:
                raise AIGuardError(503, "AI_BUDGET_UNAVAILABLE") from exc
            try:
                con.execute("BEGIN IMMEDIATE")
                con.execute("INSERT OR IGNORE INTO ai_daily_budget(day, count) VALUES (?, 0)", (day,))
                count = int(con.execute("SELECT count FROM ai_daily_budget WHERE day = ?", (day,)).fetchone()[0])
                if count >= limit:
                    con.execute("ROLLBACK")
                    raise AIGuardError(503, "AI_BUDGET_EXHAUSTED", retry_after=self._seconds_to_reset())
                if subject_cap > 0 and subject_hash:
                    con.execute("INSERT OR IGNORE INTO ai_subject_daily(day, subject, count) VALUES (?, ?, 0)", (day, subject_hash))
                    used = int(
                        con.execute("SELECT count FROM ai_subject_daily WHERE day = ? AND subject = ?", (day, subject_hash)).fetchone()[0]
                    )
                    if used >= subject_cap:
                        con.execute("ROLLBACK")
                        raise AIGuardError(429, "AI_SUBJECT_BUDGET_EXHAUSTED", retry_after=self._seconds_to_reset())
                    con.execute("UPDATE ai_subject_daily SET count = count + 1 WHERE day = ? AND subject = ?", (day, subject_hash))
                    # Subject rows are only meaningful for the current day.
                    con.execute("DELETE FROM ai_subject_daily WHERE day < ?", (day,))
                con.execute("UPDATE ai_daily_budget SET count = count + 1 WHERE day = ?", (day,))
                con.execute("COMMIT")
                return count + 1
            except AIGuardError:
                raise
            except sqlite3.Error as exc:
                with contextlib.suppress(sqlite3.Error):
                    con.execute("ROLLBACK")
                raise AIGuardError(503, "AI_BUDGET_UNAVAILABLE") from exc
            finally:
                con.close()

    def _seconds_to_reset(self) -> float:
        now = self.utcnow().astimezone(timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(1.0, (tomorrow - now).total_seconds())

    def snapshot(self, limit: int) -> dict:
        now = self.utcnow().astimezone(timezone.utc)
        day = now.strftime("%Y-%m-%d")
        resets_at = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        count: Optional[int] = 0
        with self._lock:
            try:
                con = self._connect()
                try:
                    row = con.execute("SELECT count FROM ai_daily_budget WHERE day = ?", (day,)).fetchone()
                    count = int(row[0]) if row else 0
                finally:
                    con.close()
            except (sqlite3.Error, OSError):
                count = None
        return {
            "date": day,
            "count": count,
            "limit": limit,
            "remaining": None if count is None else max(0, limit - count),
            "resets_at": resets_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "available": count is not None,
        }


# ── Upstream call: one attempt, total deadline, bounded read ─────────────────

async def post_upstream(
    url: str,
    payload: dict,
    headers: Mapping[str, str],
    *,
    deadline_sec: float,
    max_bytes: int,
    retries: int = 0,
    transport: Any = None,
) -> str:
    """POST once and return the answer text.

    ``retries`` is capped at 1 and applies only to a connection that was never
    established (``httpx.ConnectError``): nothing was sent, so nothing can have
    been billed. A timeout or any HTTP response is never retried. The whole
    call, including a permitted reconnect and the body read, shares one deadline.
    """
    import httpx

    deadline = max(1.0, min(float(deadline_sec), HARD_MAX_UPSTREAM_DEADLINE_SEC))
    attempts = 1 + max(0, min(int(retries), HARD_MAX_RETRIES))

    async def attempt() -> bytes:
        async with httpx.AsyncClient(timeout=httpx.Timeout(deadline), transport=transport) as client:
            async with client.stream("POST", url, json=payload, headers=dict(headers)) as response:
                if response.status_code >= 400:
                    retry_after = None
                    if response.status_code == 429:
                        raw_retry = response.headers.get("retry-after", "")
                        retry_after = min(float(raw_retry), 300.0) if raw_retry.isdigit() else 30.0
                    raise AIGuardError(429 if response.status_code == 429 else 502, "ai_upstream", retry_after=retry_after)
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise AIGuardError(502, "ai_oversize")
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer += chunk
                    if len(buffer) > max_bytes:
                        raise AIGuardError(502, "ai_oversize")
                return bytes(buffer)

    async def run() -> bytes:
        for index in range(attempts):
            try:
                return await attempt()
            except httpx.ConnectError as exc:
                if index + 1 < attempts:
                    continue
                raise AIGuardError(502, "ai_unreachable") from exc
        raise AIGuardError(502, "ai_unreachable")

    try:
        raw = await asyncio.wait_for(run(), timeout=deadline)
    except asyncio.TimeoutError as exc:
        raise AIGuardError(504, "ai_timeout") from exc
    except httpx.TimeoutException as exc:
        raise AIGuardError(504, "ai_timeout") from exc
    except httpx.HTTPError as exc:
        raise AIGuardError(502, "ai_unreachable") from exc

    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise AIGuardError(502, "ai_empty") from exc
    answer = None
    if isinstance(data, dict):
        answer = data.get("answer")
        if not isinstance(answer, str) and isinstance(data.get("data"), dict):
            answer = data["data"].get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise AIGuardError(502, "ai_empty")
    return answer.strip()


def cap_answer(answer: str, max_chars: int) -> str:
    if len(answer) <= max_chars:
        return answer
    return answer[:max_chars].rstrip() + ANSWER_TRUNCATION_MARK


# ── Guard: dedup, cache, concurrency, budget, logging ────────────────────────

@dataclass
class Built:
    """What a handler hands to the guard after server-side context assembly."""
    prompt: str
    finish: Callable[[str], dict]
    meta: dict = field(default_factory=dict)


class _Entry:
    __slots__ = ("task", "waiters")

    def __init__(self):
        self.task: Optional[asyncio.Future] = None
        self.waiters = 0


class Guard:
    def __init__(
        self,
        cfg: GuardConfig,
        *,
        upstream: Callable[[str], Awaitable[str]],
        log: Optional[Callable[[dict], None]] = None,
        budget: Optional[BudgetStore] = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ):
        self.cfg = cfg
        self._upstream = upstream
        self._log_fn = log
        self.clock = clock
        self.wall = wall
        self.budget = budget or BudgetStore(cfg.state_dir / "ai_budget.sqlite3")
        self.limiter = RateLimiter(cfg.rate_burst, cfg.rate_per_minute, clock=clock)
        self.session_limiter = RateLimiter(cfg.session_rate_burst, cfg.session_rate_per_minute, clock=clock)
        self.inflight: dict = {}
        self._cache: "OrderedDict[str, tuple]" = OrderedDict()
        self._sem = asyncio.Semaphore(cfg.max_concurrent)
        self._queued = 0
        self._active = 0
        self._log_tasks: set = set()
        self.counters = {
            "upstream_calls": 0, "cache_hits": 0, "joined": 0, "rate_limited": 0, "unauthorized": 0,
            "forbidden_origin": 0, "busy": 0, "budget_refused": 0, "cancelled": 0,
        }

    # -- admission ---------------------------------------------------------
    def require_enabled(self) -> None:
        if not self.cfg.enabled:
            raise AIGuardError(503, "AI_DISABLED")

    def check_origin(self, origin: Optional[str], sec_fetch_site: Optional[str]) -> None:
        """Same-origin check. This stops other sites from driving a visitor's
        browser at the endpoint; it is NOT authentication (any script can forge it)."""
        if sec_fetch_site is not None and sec_fetch_site.strip().lower() != "same-origin":
            self.counters["forbidden_origin"] += 1
            raise AIGuardError(403, "forbidden_origin")
        if origin is None or origin.strip().rstrip("/") not in self.cfg.allowed_origins:
            self.counters["forbidden_origin"] += 1
            raise AIGuardError(403, "forbidden_origin")

    def subject_for(self, peer_host: Optional[str], x_real_ip: Optional[str]) -> str:
        return client_subject(peer_host, x_real_ip, self.cfg.trusted_proxies)

    def subject_hash(self, subject: str) -> str:
        return hmac.new(self.cfg.session_key, f"subject|{subject}".encode("utf-8"), hashlib.sha256).hexdigest()[:24]

    def text_hash(self, text: str) -> str:
        return hmac.new(self.cfg.session_key, f"text|{normalize_text(text)}".encode("utf-8"), hashlib.sha256).hexdigest()[:16]

    def issue_session(self, subject: str) -> tuple:
        wait = self.session_limiter.take(subject)
        if wait > 0:
            self.counters["rate_limited"] += 1
            raise AIGuardError(429, "rate_limited", retry_after=wait)
        return issue_session_token(self.cfg.session_key, subject, self.cfg.session_ttl_sec, now=self.wall())

    def require_session(self, token: Optional[str], subject: str) -> None:
        if not verify_session_token(self.cfg.session_key, token, subject, now=self.wall()):
            self.counters["unauthorized"] += 1
            raise AIGuardError(401, "AI_SESSION_REQUIRED")

    def rate_limit(self, subject: str) -> None:
        wait = self.limiter.take(subject)
        if wait > 0:
            self.counters["rate_limited"] += 1
            raise AIGuardError(429, "rate_limited", retry_after=wait)

    # -- cache -------------------------------------------------------------
    def _cache_get(self, key: str) -> Optional[dict]:
        hit = self._cache.get(key)
        if hit is None:
            return None
        expires, value = hit
        if expires <= self.clock():
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return value

    def _cache_put(self, key: str, value: dict) -> None:
        if self.cfg.cache_ttl_sec <= 0 or self.cfg.cache_max_entries <= 0:
            return
        try:
            size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            return
        if size > self.cfg.cache_max_entry_bytes:
            return
        self._cache[key] = (self.clock() + self.cfg.cache_ttl_sec, value)
        self._cache.move_to_end(key)
        while len(self._cache) > self.cfg.cache_max_entries:
            self._cache.popitem(last=False)

    # -- concurrency -------------------------------------------------------
    @contextlib.asynccontextmanager
    async def _slot(self):
        if self._sem.locked():
            if self._queued >= self.cfg.max_queue:
                self.counters["busy"] += 1
                raise AIGuardError(503, "AI_BUSY", retry_after=5)
            self._queued += 1
            try:
                await asyncio.wait_for(self._sem.acquire(), timeout=self.cfg.queue_wait_sec)
            except asyncio.TimeoutError as exc:
                self.counters["busy"] += 1
                raise AIGuardError(503, "AI_BUSY", retry_after=5) from exc
            finally:
                self._queued -= 1
        else:
            await self._sem.acquire()
        self._active += 1
        try:
            yield
        finally:
            self._active -= 1
            self._sem.release()

    # -- shared in-flight call with waiter-aware cancellation --------------
    def _finish_entry(self, key: str, entry: _Entry, task: asyncio.Future) -> None:
        if self.inflight.get(key) is entry:
            del self.inflight[key]
        if not task.cancelled():
            task.exception()  # mark retrieved; waiters read it through result()

    async def run_shared(self, key: str, factory: Callable[[], Awaitable[dict]], gone: Optional[asyncio.Event] = None) -> dict:
        """Identical requests share one call. The call is cancelled only when
        every waiter has left, and it is removed from the in-flight map
        synchronously at that moment, so a later identical request starts a
        fresh call instead of joining a dying one."""
        if gone is not None and gone.is_set():
            self.counters["cancelled"] += 1
            raise AIGuardError(499, "cancelled")
        entry = self.inflight.get(key)
        if entry is None:
            entry = _Entry()
            entry.task = asyncio.ensure_future(factory())
            self.inflight[key] = entry
            entry.task.add_done_callback(lambda task, e=entry: self._finish_entry(key, e, task))
        else:
            self.counters["joined"] += 1
        entry.waiters += 1
        left = False

        def leave() -> None:
            nonlocal left
            if left:
                return
            left = True
            entry.waiters -= 1
            if entry.waiters <= 0 and not entry.task.done():
                if self.inflight.get(key) is entry:
                    del self.inflight[key]
                entry.task.cancel()

        gone_task = asyncio.ensure_future(gone.wait()) if gone is not None else None
        try:
            waiting = {entry.task} if gone_task is None else {entry.task, gone_task}
            await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            if entry.task.done():
                if entry.task.cancelled():
                    raise AIGuardError(499, "cancelled")
                return entry.task.result()
            self.counters["cancelled"] += 1
            raise AIGuardError(499, "cancelled")
        finally:
            leave()
            if gone_task is not None:
                gone_task.cancel()

    # -- the one paid path -------------------------------------------------
    async def handle(
        self,
        kind: str,
        fields: Mapping[str, Any],
        subject: str,
        build: Callable[[], Awaitable[Built]],
        *,
        gone: Optional[asyncio.Event] = None,
        primary_text: str = "",
        message_text: str = "",
    ) -> dict:
        key = dedup_key(kind, fields)
        cached = self._cache_get(key)
        if cached is not None:
            self.counters["cache_hits"] += 1
            return {**cached, "cached": True}
        log_base = {
            "kind": kind,
            "query_hash": self.text_hash(primary_text) if primary_text else "",
            "query_len": len(primary_text),
            "message_len": len(message_text),
            "phase": fields.get("phase"),
        }
        result = await self.run_shared(key, lambda: self._work(key, subject, build, log_base), gone)
        return {**result, "cached": False}

    async def _work(self, key: str, subject: str, build: Callable[[], Awaitable[Built]], log_base: dict) -> dict:
        started = self.clock()
        record = dict(log_base, outcome="ok", prompt_chars=0, answer_chars=0, upstream_called=False)
        try:
            async with self._slot():
                built = await build()
                record.update({k: v for k, v in (built.meta or {}).items() if k in ("subject_count", "evidence_count", "gaokao_hit_count")})
                record["prompt_chars"] = len(built.prompt)
                if len(built.prompt) > self.cfg.max_prompt_chars:
                    raise AIGuardError(413, "prompt_too_large")
                try:
                    self.budget.consume(
                        self.cfg.daily_budget,
                        subject_hash=self.subject_hash(subject),
                        subject_cap=self.cfg.subject_daily_cap,
                    )
                except AIGuardError:
                    self.counters["budget_refused"] += 1
                    raise
                self.counters["upstream_calls"] += 1
                record["upstream_called"] = True
                answer = cap_answer(await self._upstream(built.prompt), self.cfg.max_answer_chars)
                record["answer_chars"] = len(answer)
                result = built.finish(answer)
            self._cache_put(key, result)
            return result
        except AIGuardError as exc:
            record["outcome"] = exc.code
            raise
        except asyncio.CancelledError:
            record["outcome"] = "cancelled"
            raise
        except Exception as exc:  # never leak internals to the caller
            record["outcome"] = "internal_error"
            raise AIGuardError(500, "internal_error") from exc
        finally:
            record["duration_ms"] = int((self.clock() - started) * 1000)
            self._emit_log(record)

    def _emit_log(self, record: dict) -> None:
        if self._log_fn is None:
            return
        try:
            future = asyncio.get_running_loop().run_in_executor(None, self._log_fn, record)
        except RuntimeError:
            return
        self._log_tasks.add(future)
        future.add_done_callback(lambda f: (self._log_tasks.discard(f), f.cancelled() or f.exception()))

    async def drain_logs(self) -> None:
        while self._log_tasks:
            await asyncio.gather(*list(self._log_tasks), return_exceptions=True)

    # -- read-only state for the status system (no user data) --------------
    def status(self) -> dict:
        if not self.cfg.enabled:
            return {"enabled": False, "missing": self.cfg.disabled_reasons(), "budget": None}
        return {
            "enabled": True,
            "budget": self.budget.snapshot(self.cfg.daily_budget),
            "subject_daily_cap": self.cfg.subject_daily_cap,
            "limits": self.cfg.public_limits(),
            "runtime": {"inflight": len(self.inflight), "active": self._active, "queued": self._queued, "cached": len(self._cache)},
            "counters": dict(self.counters),
        }


# ── ai_chat_logs: minimised, additive-only ───────────────────────────────────

AI_CHAT_LOG_ADDITIVE_COLUMNS = (
    ("kind", "TEXT"),
    ("outcome", "TEXT"),
    ("query_hash", "TEXT"),
    ("query_len", "INTEGER"),
    ("message_len", "INTEGER"),
    ("prompt_chars", "INTEGER"),
    ("answer_chars", "INTEGER"),
    ("duration_ms", "INTEGER"),
    ("upstream_called", "INTEGER"),
)


def ensure_chat_log_columns(con: sqlite3.Connection) -> list:
    """Adds the minimised columns to an existing ``ai_chat_logs`` table.
    Additive only: no column is dropped or rewritten, old rows are untouched."""
    existing = {row[1] for row in con.execute("PRAGMA table_info(ai_chat_logs)").fetchall()}
    added = []
    for name, sql_type in AI_CHAT_LOG_ADDITIVE_COLUMNS:
        if name not in existing:
            con.execute(f"ALTER TABLE ai_chat_logs ADD COLUMN {name} {sql_type}")
            added.append(name)
    return added


def write_chat_log(con: sqlite3.Connection, record: Mapping[str, Any], *, provider: str = "", now: Optional[float] = None) -> None:
    """One row per shared upstream attempt, written by the server itself.

    Raw user text is NOT stored: the legacy NOT NULL ``query`` / ``user_message``
    columns receive empty strings; ``error`` holds only a coarse outcome code.
    """
    outcome = str(record.get("outcome") or "ok")[:40]
    con.execute(
        """
        INSERT INTO ai_chat_logs (
            query, user_message, subject_count, evidence_count, gaokao_hit_count,
            provider, success, error, phase, ts,
            kind, outcome, query_hash, query_len, message_len,
            prompt_chars, answer_chars, duration_ms, upstream_called
        ) VALUES ('', '', ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(record.get("subject_count") or 0),
            int(record.get("evidence_count") or 0),
            int(record.get("gaokao_hit_count") or 0),
            str(provider or "")[:40],
            1 if outcome == "ok" else 0,
            None if outcome == "ok" else outcome,
            record.get("phase"),
            time.time() if now is None else now,
            str(record.get("kind") or "")[:20],
            outcome,
            str(record.get("query_hash") or "")[:32],
            int(record.get("query_len") or 0),
            int(record.get("message_len") or 0),
            int(record.get("prompt_chars") or 0),
            int(record.get("answer_chars") or 0),
            int(record.get("duration_ms") or 0),
            1 if record.get("upstream_called") else 0,
        ),
    )


def prune_chat_logs(con: sqlite3.Connection, *, older_than_days: int, limit: int = 5000, apply: bool = False, now: Optional[float] = None) -> dict:
    """Bounded retention pass over ``ai_chat_logs``. NOT scheduled anywhere:
    an operator runs it deliberately (see docs/AI_GUARD.md). Dry-run by default."""
    if older_than_days < 1:
        raise ValueError("older_than_days must be >= 1")
    limit = max(1, min(int(limit), 50000))
    cutoff = (time.time() if now is None else now) - older_than_days * 86400
    eligible = int(con.execute("SELECT COUNT(*) FROM ai_chat_logs WHERE ts < ?", (cutoff,)).fetchone()[0])
    deleted = 0
    if apply and eligible:
        cursor = con.execute(
            "DELETE FROM ai_chat_logs WHERE id IN (SELECT id FROM ai_chat_logs WHERE ts < ? ORDER BY ts ASC LIMIT ?)",
            (cutoff, limit),
        )
        deleted = cursor.rowcount
        con.commit()
    return {"cutoff_ts": cutoff, "eligible": eligible, "deleted": deleted, "applied": bool(apply), "batch_limit": limit}


def redact_legacy_chat_log_text(con: sqlite3.Connection, *, limit: int = 5000, apply: bool = False) -> dict:
    """Optional, operator-run: blank the raw text kept by rows written before the
    guard (rows whose ``kind`` is NULL). Bounded, dry-run by default, never scheduled."""
    limit = max(1, min(int(limit), 50000))
    columns = {row[1] for row in con.execute("PRAGMA table_info(ai_chat_logs)").fetchall()}
    if apply and "kind" not in columns:
        ensure_chat_log_columns(con)
        columns.add("kind")
    where = "(query <> '' OR user_message <> '')" + (" AND kind IS NULL" if "kind" in columns else "")
    eligible = int(con.execute(f"SELECT COUNT(*) FROM ai_chat_logs WHERE {where}").fetchone()[0])
    changed = 0
    if apply and eligible:
        cursor = con.execute(
            "UPDATE ai_chat_logs SET query_len = length(query), message_len = length(user_message), "
            "query = '', user_message = '', error = CASE WHEN error IS NULL THEN NULL ELSE 'legacy_error' END "
            f"WHERE id IN (SELECT id FROM ai_chat_logs WHERE {where} ORDER BY id ASC LIMIT ?)",
            (limit,),
        )
        changed = cursor.rowcount
        con.commit()
    return {"eligible": eligible, "changed": changed, "applied": bool(apply), "batch_limit": limit}


# ── HTTP layer ───────────────────────────────────────────────────────────────

async def read_capped_json(request: Any, max_bytes: int) -> Any:
    """Streamed body read with a byte cap. A missing or non-numeric
    Content-Length is rejected before a single body byte is read."""
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise AIGuardError(415, "unsupported_media_type")
    declared = request.headers.get("content-length")
    if declared is None or not declared.strip().isdigit():
        raise AIGuardError(411, "length_required")
    if int(declared) > max_bytes:
        raise AIGuardError(413, "body_too_large")
    buffer = bytearray()
    async for chunk in request.stream():
        buffer += chunk
        if len(buffer) > max_bytes:
            raise AIGuardError(413, "body_too_large")
    try:
        return json.loads(buffer.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AIGuardError(400, "bad_json") from exc


async def _watch_disconnect(request: Any, gone: asyncio.Event) -> None:
    # The body has been consumed, so the next ASGI message is the disconnect.
    try:
        while True:
            message = await request.receive()
            if message.get("type") == "http.disconnect":
                gone.set()
                return
    except Exception:
        gone.set()


def build_router(guard: Guard, builders: Mapping[str, Callable[[dict], Awaitable[Built]]]):
    """FastAPI router for every AI endpoint.

    ``builders`` maps ``chat`` / ``dict`` / ``gaokao`` to an async function that
    takes the validated fields and returns ``Built`` (server-side retrieval +
    prompt). No builder runs before admission, caps and rate limiting pass.
    """
    # NOTE: this module must not use ``from __future__ import annotations``: FastAPI has to
    # resolve the locally imported ``Request`` annotation on the route handlers below.
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse

    router = APIRouter()
    no_store = {"Cache-Control": "no-store"}

    def error_response(exc: AIGuardError) -> JSONResponse:
        headers = dict(no_store)
        if exc.retry_after is not None:
            headers["Retry-After"] = str(exc.retry_after)
        return JSONResponse(exc.body(), status_code=exc.status, headers=headers)

    def subject_of(request: Request) -> str:
        peer = request.client.host if request.client else None
        return guard.subject_for(peer, request.headers.get("x-real-ip"))

    async def guarded(request: Request, kind: str, validate: Callable[[Any, GuardConfig], dict], primary: str) -> JSONResponse:
        try:
            guard.require_enabled()
            guard.check_origin(request.headers.get("origin"), request.headers.get("sec-fetch-site"))
            subject = subject_of(request)
            guard.require_session(request.cookies.get(SESSION_COOKIE), subject)
            guard.rate_limit(subject)
            fields = validate(await read_capped_json(request, guard.cfg.max_body_bytes), guard.cfg)
            gone = asyncio.Event()
            watcher = asyncio.ensure_future(_watch_disconnect(request, gone))
            try:
                result = await guard.handle(
                    kind,
                    fields,
                    subject,
                    lambda: builders[kind](fields),
                    gone=gone,
                    primary_text=str(fields.get(primary) or ""),
                    message_text=str(fields.get("user_message") or ""),
                )
            finally:
                watcher.cancel()
            return JSONResponse(result, headers=no_store)
        except AIGuardError as exc:
            return error_response(exc)

    @router.post("/api/ai/session")
    async def ai_session(request: Request):
        """Cheap same-origin admission. Abuse friction and a quota subject, not user authentication."""
        try:
            guard.require_enabled()
            guard.check_origin(request.headers.get("origin"), request.headers.get("sec-fetch-site"))
            token, expires = guard.issue_session(subject_of(request))
        except AIGuardError as exc:
            return error_response(exc)
        response = JSONResponse({"ok": True, "expires_at": expires, "limits": guard.cfg.public_limits()}, headers=no_store)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=guard.cfg.session_ttl_sec,
            path="/api",
            httponly=True,
            secure=guard.cfg.cookie_secure,
            samesite="strict",
        )
        return response

    @router.get("/api/ai/status")
    async def ai_status():
        """Read-only budget state for the status system: counters only, no user data."""
        return JSONResponse(guard.status(), headers=no_store)

    @router.post("/api/chat")
    async def chat(request: Request):
        return await guarded(request, "chat", validate_chat, "query")

    @router.post("/api/dict/chat")
    async def dict_chat(request: Request):
        return await guarded(request, "dict", validate_dict_chat, "headword")

    @router.post("/api/gaokao/chat")
    async def gaokao_chat(request: Request):
        return await guarded(request, "gaokao", validate_gaokao_chat, "")

    # Retired: the client no longer runs a second retrieval or reports its own telemetry.
    # The handlers take no body parameter, so nothing the caller sends is read or stored.
    @router.post("/api/chat/context")
    async def chat_context_retired():
        return error_response(AIGuardError(410, "retired"))

    @router.post("/api/chat/log")
    async def chat_log_retired():
        return error_response(AIGuardError(410, "retired"))

    return router


# ── Operator CLI (never scheduled) ───────────────────────────────────────────

def _main(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="ai_chat_logs retention tools (dry-run unless --apply)")
    sub = parser.add_subparsers(dest="command", required=True)
    prune = sub.add_parser("prune-logs", help="delete ai_chat_logs rows older than N days, in one bounded batch")
    prune.add_argument("--db", required=True)
    prune.add_argument("--older-than-days", type=int, required=True)
    prune.add_argument("--limit", type=int, default=5000)
    prune.add_argument("--apply", action="store_true")
    redact = sub.add_parser("redact-legacy-text", help="blank raw text in pre-guard rows, in one bounded batch")
    redact.add_argument("--db", required=True)
    redact.add_argument("--limit", type=int, default=5000)
    redact.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    con = sqlite3.connect(args.db, timeout=30)
    try:
        if args.command == "prune-logs":
            report = prune_chat_logs(con, older_than_days=args.older_than_days, limit=args.limit, apply=args.apply)
        else:
            report = redact_legacy_chat_log_text(con, limit=args.limit, apply=args.apply)
    finally:
        con.close()
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
