"""Bounded page images made from the catalogued PDFs (SUN library).

    GET /files/page/{edition_id}/{sha12}/{pdf_page}.webp?w=240|480|960|1440
    GET /files/cover/{edition_id}.webp?w=240|480
    GET /files/legacy-page/{jc_short_key}/p{index}.webp?w=      (302 to /files/page/…)
    GET /api/library/legacy-page?jc=&index=&w=                  (JSON)

Why it is built this way (host: 5.9 GiB RAM, no swap, one uvicorn worker):

  * the web process never imports PyMuPDF.  A render is one short-lived child
    process (page_render_worker.py) with a time limit and, on Linux, an address-
    space cap.  Nothing a PDF can do in the renderer reaches the application;
  * at most ONE render runs at a time; at most `queue` more wait; beyond that the
    answer is 503 + Retry-After.  Identical concurrent requests share one render;
  * results live in a disk cache keyed by the PDF's sha256, page, width and
    RENDER_VERSION, written atomically, capped in total size and evicted
    least-recently-used with bounded work.  A cache hit touches no renderer;
  * only pages of editions held on this site in the loaded catalog release can be
    asked for, in a fixed set of widths.  No request value reaches a path.

Only FastAPI/Starlette and the standard library are imported here.
"""
from __future__ import annotations

import hashlib
import os
import stat as stat_module
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

RENDER_VERSION = "r1"                     # bump when rendering parameters change: old files are evicted
WIDTHS = (240, 480, 960, 1440)
DEFAULT_WIDTH = 960
COVER_WIDTHS = (240, 480)
MAX_OUTPUT_PIXELS = 1440 * 2200           # 3,168,000 px for one rendered page
WEBP_QUALITY = 80
MAX_COVER_FILE_BYTES = 512 * 1024
MAX_IMAGE_FILE_BYTES = 8 * 1024 * 1024    # a rendered file larger than this is not served from memory
WORKER = Path(__file__).resolve().with_name("page_render_worker.py")

PAGE_NAME_RE = re.compile(r"^([1-9][0-9]{0,4})\.webp$")
LEGACY_NAME_RE = re.compile(r"^p([0-9]{1,5})\.webp$")
COVER_NAME_RE = re.compile(r"^([a-z0-9][a-z0-9._-]{2,95})\.webp$")
SHORT_KEY_RE = re.compile(r"^[0-9a-f]{12}$")
SHA12_RE = re.compile(r"^[0-9a-f]{12}$")
ORIGIN_RE = re.compile(r"^https://(?:[a-z0-9-]+\.)*(?:bdfz\.net|rdfz\.net|rdfzer\.com)$")

WORKER_ERRORS = {3: ("unreadable", 500), 4: ("page-out-of-range", 404), 5: ("page-size-not-acceptable", 422),
                 6: ("out-of-memory", 500)}


class RenderBusy(Exception):
    """The single render slot and its queue are full, or the wait ran out."""


class RenderFailed(Exception):
    def __init__(self, reason: str, status: int = 500):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _int_env(env: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(str(env.get(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def _float_env(env: Mapping[str, str], name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(str(env.get(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


@dataclass(frozen=True)
class ImageSettings:
    cache_root: Path
    cache_max_bytes: int
    accel_prefix: str
    render_timeout: float
    queue: int
    wait_timeout: float
    memory_mb: int
    image_burst: int
    image_per_second: float
    render_burst: int
    render_per_second: float
    cors_origins: tuple[str, ...]
    waiters: int = 8

    @classmethod
    def from_env(cls, env: Mapping[str, str], serve_mode: str, state_root: Path) -> "ImageSettings":
        # In accel mode nginx serves cache hits, and nginx cannot read /root (where the
        # app's state directory lives on the host): the cache must sit under /srv.
        default_root = "/srv/sun-textbooks/page-cache" if serve_mode == "accel" else str(Path(state_root) / "page-cache")
        prefix = str(env.get("LIBRARY_PAGE_ACCEL_PREFIX", "/_protected_page/")).strip() or "/_protected_page/"
        if not re.fullmatch(r"/[A-Za-z0-9_./-]*", prefix):
            prefix = "/_protected_page/"
        if not prefix.endswith("/"):
            prefix += "/"
        origins = tuple(dict.fromkeys(
            item.strip().lower().rstrip("/") for item in str(env.get("LIBRARY_CORS_ORIGINS", "")).split(",")
            if ORIGIN_RE.fullmatch(item.strip().lower().rstrip("/"))))
        return cls(
            cache_root=Path(env.get("LIBRARY_PAGE_CACHE_ROOT") or default_root),
            cache_max_bytes=_int_env(env, "LIBRARY_PAGE_CACHE_MAX_BYTES", 2 * 1024 ** 3, 16 * 1024 ** 2, 64 * 1024 ** 3),
            accel_prefix=prefix,
            render_timeout=_float_env(env, "LIBRARY_RENDER_TIMEOUT_SECONDS", 20.0, 1.0, 120.0),
            queue=_int_env(env, "LIBRARY_RENDER_QUEUE", 8, 0, 64),
            wait_timeout=_float_env(env, "LIBRARY_RENDER_WAIT_SECONDS", 25.0, 1.0, 180.0),
            memory_mb=_int_env(env, "LIBRARY_RENDER_MEMORY_MB", 1536, 0, 8192),
            image_burst=_int_env(env, "LIBRARY_RATE_IMAGE_BURST", 600, 1, 100000),
            image_per_second=_float_env(env, "LIBRARY_RATE_IMAGE_PER_SECOND", 20.0, 0.01, 10000.0),
            render_burst=_int_env(env, "LIBRARY_RATE_RENDER_BURST", 20, 1, 100000),
            render_per_second=_float_env(env, "LIBRARY_RATE_RENDER_PER_SECOND", 2.0, 0.01, 10000.0),
            cors_origins=origins,
            # Requests allowed to WAIT for a render at the same time (each holds one of the
            # server's ~40 worker threads); the rest are answered 503 at once.
            waiters=_int_env(env, "LIBRARY_RENDER_WAITERS", 8, 1, 24),
        )


# ── Disk cache ───────────────────────────────────────────────────────────────

class PageCache:
    """<root>/<RENDER_VERSION>/<pdf sha256>/<page>-<width>.webp

    Files are written to a temp name and renamed.  The total is tracked in memory
    after one scan; when it passes the cap, the least recently used files (other
    render versions first) are removed, at most `EVICT_BATCH` per pass."""

    EVICT_BATCH = 400
    TOUCH_AFTER = 3600.0
    STALE_TMP = 3600.0

    def __init__(self, root: Path, max_bytes: int):
        self.root = Path(root)
        self.max_bytes = int(max_bytes)
        self._lock = threading.Lock()
        self._total: Optional[int] = None
        self._counter = 0
        self.evictions = 0

    def relative(self, sha256: str, page: int, width: int) -> str:
        return f"{RENDER_VERSION}/{sha256}/{page}-{width}.webp"

    def path_for(self, sha256: str, page: int, width: int) -> Path:
        return self.root / RENDER_VERSION / sha256 / f"{page}-{width}.webp"

    def lookup(self, path: Path) -> Optional[os.stat_result]:
        try:
            stat = os.lstat(path)
        except OSError:
            return None
        if not stat_module.S_ISREG(stat.st_mode) or stat.st_size <= 0:   # never a symlink
            return None
        now = time.time()
        if now - stat.st_atime > self.TOUCH_AFTER:   # keep LRU order honest on noatime/relatime mounts
            try:
                os.utime(path, (now, stat.st_mtime))
            except OSError:
                pass
        return stat

    def temp_path(self, final: Path) -> Path:
        with self._lock:
            self._counter += 1
            counter = self._counter
        final.parent.mkdir(parents=True, exist_ok=True)
        for directory in (final.parent, final.parent.parent, self.root):
            try:
                os.chmod(directory, 0o755)    # nginx (another user) must be able to traverse
            except OSError:
                pass
        return final.parent / f".{final.name}.tmp-{os.getpid()}-{counter}"

    def commit(self, temp: Path, final: Path) -> int:
        size = os.path.getsize(temp)
        os.chmod(temp, 0o644)
        os.replace(temp, final)               # atomic: readers see the old state or the whole file
        with self._lock:
            if self._total is not None:
                self._total += size
        return size

    def _scan(self) -> list[tuple[int, float, int, str]]:
        """(is_current_version, last_use, size, path) for every cached file; removes stale temp files."""
        entries = []
        now = time.time()
        try:
            versions = list(os.scandir(self.root))
        except OSError:
            return entries
        for version in versions:
            if not version.is_dir(follow_symlinks=False):
                continue
            current = 1 if version.name == RENDER_VERSION else 0
            try:
                books = list(os.scandir(version.path))
            except OSError:
                continue
            for book in books:
                if not book.is_dir(follow_symlinks=False):
                    continue
                try:
                    files = list(os.scandir(book.path))
                except OSError:
                    continue
                for item in files:
                    try:
                        stat = item.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if item.name.startswith("."):
                        if now - stat.st_mtime > self.STALE_TMP:
                            try:
                                os.unlink(item.path)
                            except OSError:
                                pass
                        continue
                    entries.append((current, max(stat.st_atime, stat.st_mtime), stat.st_size, item.path))
        return entries

    def total_bytes(self) -> int:
        with self._lock:
            known = self._total
        if known is None:
            known = sum(size for _, _, size, _ in self._scan())
            with self._lock:
                if self._total is None:
                    self._total = known
                known = self._total
        return known

    def evict_if_needed(self) -> int:
        """Called from the render thread after a write.  Bounded work: one scan, at most
        EVICT_BATCH deletions.  Brings the total down to 90 % of the cap."""
        if self.total_bytes() <= self.max_bytes:
            return 0
        entries = self._scan()
        total = sum(size for _, _, size, _ in entries)
        target = int(self.max_bytes * 0.9)
        removed = 0
        entries.sort(key=lambda entry: (entry[0], entry[1]))   # other render versions first, then oldest use
        for _, _, size, path in entries:
            if total <= target or removed >= self.EVICT_BATCH:
                break
            try:
                os.unlink(path)
            except OSError:
                continue
            total -= size
            removed += 1
        with self._lock:
            self._total = total
            self.evictions += removed
        return removed


# ── Render service ───────────────────────────────────────────────────────────

def subprocess_runner(pdf_path: Path, page: int, width: int, out_path: Path, settings: ImageSettings) -> None:
    """Render in a child process; kill it at the time limit."""
    command = [sys.executable, "-I", str(WORKER), str(pdf_path), str(page), str(width), str(out_path),
               str(MAX_OUTPUT_PIXELS), str(WEBP_QUALITY), str(settings.memory_mb)]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=settings.render_timeout, close_fds=True,
                                env={"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        raise RenderFailed("timeout", 504)
    except OSError:
        raise RenderFailed("renderer-unavailable", 503)
    if result.returncode != 0:
        reason, status = WORKER_ERRORS.get(result.returncode, ("render-error", 500))
        raise RenderFailed(reason, status)


class RenderService:
    FAILURE_TTL = 600.0

    def __init__(self, settings: ImageSettings, runner: Callable = subprocess_runner):
        self.settings = settings
        self.cache = PageCache(settings.cache_root, settings.cache_max_bytes)
        self.runner = runner
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="page-render")
        self._lock = threading.Lock()
        self._inflight: dict[tuple, Future] = {}
        self._failures: dict[tuple, tuple[float, str, int]] = {}
        self._waiters = threading.BoundedSemaphore(settings.waiters)
        self.renders = 0

    def stats(self) -> dict:
        with self._lock:
            return {"inflight": len(self._inflight), "renders": self.renders, "evictions": self.cache.evictions}

    def ensure(self, pdf_path: Path, sha256: str, page: int, width: int) -> Path:
        """Blocking (call from a worker thread).  Returns the cached file, rendering it once if needed."""
        final = self.cache.path_for(sha256, page, width)
        key = (sha256, page, width)
        with self._lock:
            failed = self._failures.get(key)
            if failed is not None:
                if time.monotonic() - failed[0] < self.FAILURE_TTL:
                    raise RenderFailed(failed[1], failed[2])   # a page that cannot be rendered is not retried at once
                del self._failures[key]
            future = self._inflight.get(key)
            if future is None:
                if len(self._inflight) > self.settings.queue:   # 1 running + `queue` waiting
                    raise RenderBusy()
                future = self._executor.submit(self._render, pdf_path, key, final)
                self._inflight[key] = future
        try:
            future.result(timeout=self.settings.wait_timeout)
        except FutureTimeout:
            raise RenderBusy()
        return final

    def ensure_bounded(self, pdf_path: Path, sha256: str, page: int, width: int) -> Path:
        """ensure() for request handlers: at most `waiters` callers may block here at once, so
        slow renders can never use up the server's worker threads.  The rest get RenderBusy."""
        if not self._waiters.acquire(blocking=False):
            raise RenderBusy()
        try:
            return self.ensure(pdf_path, sha256, page, width)
        finally:
            self._waiters.release()

    def _render(self, pdf_path: Path, key: tuple, final: Path) -> None:
        try:
            if self.cache.lookup(final) is not None:
                return
            try:
                temp = self.cache.temp_path(final)
            except OSError:
                raise RenderFailed("cache-unwritable", 503)     # e.g. the cache root is read-only
            try:
                self.runner(pdf_path, key[1], key[2], temp, self.settings)
                if not temp.is_file() or temp.stat().st_size <= 0:
                    raise RenderFailed("render-error", 500)
                try:
                    self.cache.commit(temp, final)
                except OSError:
                    raise RenderFailed("cache-unwritable", 503)
            except RenderFailed as error:
                # Remember only failures that will repeat (bad page, page too large).  A timeout
                # or an unavailable renderer/cache may well succeed next time.
                if error.status not in (503, 504):
                    with self._lock:
                        if len(self._failures) > 500:
                            self._failures.clear()
                        self._failures[key] = (time.monotonic(), error.reason, error.status)
                raise
            finally:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
            with self._lock:
                self.renders += 1
            try:
                self.cache.evict_if_needed()
            except OSError:
                pass
        finally:
            with self._lock:
                self._inflight.pop(key, None)


# ── URL helpers (also used by main.py through library.py) ────────────────────

def page_url(edition_id: str, sha12: str, pdf_page: int, width: int = DEFAULT_WIDTH) -> str:
    return f"/files/page/{edition_id}/{sha12}/{int(pdf_page)}.webp?w={int(width)}"


def cover_url(edition_id: str, width: int = 240) -> str:
    return f"/files/cover/{edition_id}.webp?w={int(width)}"


def viewer_url(stage: Optional[str], edition_id: str, pdf_page: int) -> str:
    page = "/chuzhong.html" if stage == "初中" else "/"
    return f"{page}?view=pdf&edition={quote(edition_id, safe='')}" + (f"&p={int(pdf_page)}" if pdf_page and pdf_page > 1 else "")


def parse_width(value: Optional[str], allowed: tuple[int, ...], default: int) -> int:
    if value is None or value == "":
        return default
    if not re.fullmatch(r"[0-9]{1,5}", value) or int(value) not in allowed:
        raise HTTPException(status_code=400, detail=f"w must be one of {', '.join(map(str, allowed))}",
                            headers={"Cache-Control": "no-store"})
    return int(value)


# ── Routes ───────────────────────────────────────────────────────────────────

def build_router(lib: Any, images: ImageSettings, service: RenderService, image_limiter: Any, render_limiter: Any) -> APIRouter:
    """`lib` is library.Library (release state, allow-list helpers, limiter plumbing)."""
    router = APIRouter()
    no_store = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}

    def not_found(detail: str = "not found") -> HTTPException:
        return HTTPException(status_code=404, detail=detail, headers={"Cache-Control": "no-store"})

    def held_edition(release, edition_id: str):
        edition = release.editions.get(edition_id) if isinstance(edition_id, str) else None
        if edition is None or not edition.previewable:      # previewable == held on this site (holding "sun")
            raise not_found()
        return edition

    def send_image(request: Request, path: Path, stat: os.stat_result, relative: str, etag: str, release_id: str) -> Response:
        headers = {"X-Content-Type-Options": "nosniff", "X-Library-Release": release_id, "ETag": etag}
        if any(part.strip() in (etag, "W/" + etag) for part in (request.headers.get("if-none-match") or "")[:2000].split(",")):
            return Response(status_code=304, headers=headers)
        if lib.settings.serve_mode == "accel":
            # No Cache-Control here: nginx keeps the upstream's across the internal redirect, and
            # if the file was evicted meanwhile its 404 would carry a one-year lifetime.  The
            # internal location adds the header itself, on successful answers only.
            headers["X-Accel-Redirect"] = images.accel_prefix + relative
            return Response(status_code=200, media_type="image/webp", headers=headers)
        headers["Cache-Control"] = "public, max-age=31536000, immutable"   # 200 only; the URL is content-versioned
        headers["Content-Length"] = str(stat.st_size)
        if request.method == "HEAD":
            return Response(status_code=200, media_type="image/webp", headers=headers)
        if stat.st_size > MAX_IMAGE_FILE_BYTES:
            raise HTTPException(status_code=500, detail="image too large", headers={"Cache-Control": "no-store"})
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as handle:
                data = handle.read(MAX_IMAGE_FILE_BYTES + 1)
        except OSError:
            raise not_found("image missing")
        headers["Content-Length"] = str(len(data))
        return Response(content=data, status_code=200, media_type="image/webp", headers=headers)

    @router.api_route("/files/page/{edition_id}/{sha12}/{page_name}", methods=["GET", "HEAD"])
    def library_page_image(request: Request, edition_id: str, sha12: str, page_name: str, w: Optional[str] = None):
        lib._limit(request, image_limiter)
        release = lib._release()
        match = PAGE_NAME_RE.fullmatch(page_name or "")
        if not match or not SHA12_RE.fullmatch(sha12 or ""):
            raise not_found()
        edition = held_edition(release, edition_id)
        page = int(match.group(1))
        if edition.sha12 != sha12 or page > edition.pages:
            raise not_found()
        width = parse_width(w, WIDTHS, DEFAULT_WIDTH)
        final = service.cache.path_for(edition.sha256, page, width)
        relative = service.cache.relative(edition.sha256, page, width)
        etag = f'"{edition.sha256[:32]}-{page}-{width}-{RENDER_VERSION}"'
        stat = service.cache.lookup(final)
        if stat is None:
            # Only a miss costs a render: it has its own, much smaller, per-client budget.
            lib._limit(request, render_limiter)
            pdf_path = lib.settings.pdf_root / f"{edition.sha256}.pdf"     # derived from the catalog only
            if not pdf_path.is_file():
                raise not_found("file missing")
            try:
                service.ensure_bounded(pdf_path, edition.sha256, page, width)
            except RenderBusy:
                raise HTTPException(status_code=503, detail="页面图片正在排队生成，请稍后再试",
                                    headers={"Retry-After": "3", "Cache-Control": "no-store"})
            except RenderFailed as error:
                raise HTTPException(status_code=error.status, detail=f"render failed: {error.reason}",
                                    headers={"Cache-Control": "no-store",
                                             **({"Retry-After": "30"} if error.status == 503 else {})})
            stat = service.cache.lookup(final)
            if stat is None:
                raise HTTPException(status_code=500, detail="render failed", headers={"Cache-Control": "no-store"})
        return send_image(request, final, stat, relative, etag, release.release_id)

    @router.api_route("/files/cover/{name}", methods=["GET", "HEAD"])
    def library_cover(request: Request, name: str, w: Optional[str] = None):
        lib._limit(request, image_limiter)
        release = lib._release()
        match = COVER_NAME_RE.fullmatch(name or "")
        if not match:
            raise not_found()
        edition = held_edition(release, match.group(1))
        width = parse_width(w, COVER_WIDTHS, COVER_WIDTHS[0])
        prebuilt = (edition.record.get("cover_files") or {}).get(width)
        if prebuilt is None:
            # No pre-rendered cover in this release: the first page from the renderer.
            return Response(status_code=302, headers={
                "Location": page_url(edition.edition_id, edition.sha12, 1, width),
                "Cache-Control": "public, max-age=3600", "X-Library-Release": release.release_id,
                "X-Content-Type-Options": "nosniff"})
        path, size, digest = prebuilt
        etag = f'"cover-{digest[:32]}-{width}"'
        headers = {"X-Content-Type-Options": "nosniff", "X-Library-Release": release.release_id, "ETag": etag}
        if any(part.strip() in (etag, "W/" + etag) for part in (request.headers.get("if-none-match") or "")[:2000].split(",")):
            return Response(status_code=304, headers=headers)
        headers["Cache-Control"] = "public, max-age=604800"
        try:
            data = Path(path).read_bytes() if request.method != "HEAD" else b""
        except OSError:
            raise not_found("cover missing")
        headers["Content-Length"] = str(size if request.method == "HEAD" else len(data))
        return Response(content=data, status_code=200, media_type="image/webp", headers=headers)

    def resolve_legacy(release, jc: str, index: int) -> dict:
        """reader book + page-map row → page; else the release's nearest-mapped hint; else nothing."""
        ids = release.by_short_key.get(jc, ())
        outcome = lib._resolve_one(release, ids, index)
        if outcome.get("available"):
            edition = release.editions[outcome["edition_id"]]
            return {"edition": edition, "pdf_page": outcome["pdf_page"], "exact": True, "outcome": outcome}
        if outcome.get("reason") == "page-not-mapped":
            near = lib._nearest(release, ids, index)
            if near is not None:
                edition, pdf_page, distance, near_index = near
                return {"edition": edition, "pdf_page": pdf_page, "exact": False, "distance": distance,
                        "nearest_index": near_index, "outcome": {**lib._which(edition),
                                                                 "text_identity": edition.record["corpus"]["text_identity"]}}
        return {"edition": None, "reason": outcome.get("reason") or "unknown-book"}

    @router.api_route("/files/legacy-page/{jc}/{name}", methods=["GET", "HEAD"])
    def library_legacy_page(request: Request, jc: str, name: str, w: Optional[str] = None, nearest: Optional[str] = None):
        """Drop-in for https://img.rdfzer.com/pages/<jc>/p<index>.webp: answers 302 to the page image.
        An index without a row is 404: an <img> on another site cannot see a response header, so
        a different page is never substituted silently.  ?nearest=1 opts in to the nearest page."""
        lib._limit(request, image_limiter)
        release = lib._release()
        match = LEGACY_NAME_RE.fullmatch(name or "")
        if not match or not SHORT_KEY_RE.fullmatch(jc or ""):
            raise not_found()
        width = parse_width(w, WIDTHS, DEFAULT_WIDTH)
        found = resolve_legacy(release, jc, int(match.group(1)))
        if found["edition"] is not None and not found["exact"] and nearest != "1":
            return JSONResponse({"available": False, "reason": "page-not-mapped", "nearest_available": True,
                                 "release_id": release.release_id}, status_code=404, headers=no_store)
        if found["edition"] is None:
            return JSONResponse({"available": False, "reason": found["reason"], "release_id": release.release_id},
                                status_code=404, headers=no_store)
        edition = found["edition"]
        headers = {"Location": page_url(edition.edition_id, edition.sha12, found["pdf_page"], width),
                   "Cache-Control": "public, max-age=3600", "X-Library-Release": release.release_id,
                   "X-Content-Type-Options": "nosniff"}
        if not found["exact"]:
            headers["X-Sun-Legacy-Approximate"] = f"nearest; distance={found['distance']}"
        elif found["outcome"].get("legacy_content_revised"):
            # same position in the book, but the current edition's text on that page was revised
            headers["X-Sun-Legacy-Content-Revised"] = "1"
        return Response(status_code=302, headers=headers)

    @router.get("/api/library/legacy-page")
    def library_legacy_page_json(request: Request, jc: Optional[str] = None, index: Optional[str] = None,
                                 w: Optional[str] = None):
        lib._limit(request, lib.json_limiter)
        if not SHORT_KEY_RE.fullmatch(jc or ""):
            raise HTTPException(status_code=400, detail="jc must be a 12-hex reader key")
        if not re.fullmatch(r"[0-9]{1,5}", index or ""):
            raise HTTPException(status_code=400, detail="index must be a non-negative integer")
        width = parse_width(w, WIDTHS, DEFAULT_WIDTH)
        release = lib._release()
        found = resolve_legacy(release, jc, int(index))
        if found["edition"] is None:
            payload = {"available": False, "exact": False, "reason": found["reason"]}
        else:
            edition = found["edition"]
            payload = {
                "available": True, "exact": found["exact"], "edition_id": edition.edition_id, "pdf_page": found["pdf_page"],
                "pdf_pages": edition.pages, "title": edition.title,
                "image_url": page_url(edition.edition_id, edition.sha12, found["pdf_page"], width),
                "viewer_url": viewer_url(edition.stage, edition.edition_id, found["pdf_page"]),
                "text_identity": found["outcome"].get("text_identity"),
                "preview_default": found["outcome"].get("preview_default"),
                "default_edition_id": found["outcome"].get("default_edition_id"),
                "provenance": edition.record.get("provenance"),
                "legacy_basis": found["outcome"].get("legacy_basis") if found["exact"] else None,
                "legacy_content_revised": bool(found["exact"] and found["outcome"].get("legacy_content_revised")),
            }
            if not found["exact"]:
                payload["approximate"] = {"kind": "nearest", "distance": found["distance"],
                                          "legacy_image_index": found["nearest_index"]}
        response = lib._json(payload, release)
        origin = (request.headers.get("origin") or "").strip().lower().rstrip("/")
        response.headers["Vary"] = "Origin"
        if origin and origin in images.cors_origins:      # explicit allow-list only; the app's global CORS is untouched
            response.headers["Access-Control-Allow-Origin"] = origin
        return response

    return router


def cover_files_for(raw_cover: Any, edition_id: str, release_dir: Path, listed: set) -> dict:
    """Validated pre-rendered covers of one edition: {width: (path, size, sha256-or-size-tag)}.
    Anything unexpected is ignored (the endpoint then falls back to rendering page 1)."""
    out: dict[int, tuple[str, int, str]] = {}
    if not isinstance(raw_cover, dict):
        return out
    digests = raw_cover.get("sha256") if isinstance(raw_cover.get("sha256"), dict) else {}
    for width in COVER_WIDTHS:
        relative = raw_cover.get(f"w{width}")
        if relative != f"covers/{edition_id}-{width}.webp" or relative not in listed:
            continue     # must be the canonical name AND covered (verified) by SHA256SUMS
        path = release_dir / "covers" / f"{edition_id}-{width}.webp"
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if not 0 < size <= MAX_COVER_FILE_BYTES:
            continue
        digest = digests.get(f"w{width}")
        if not (isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)):
            digest = hashlib.sha256(f"{edition_id}:{width}:{size}".encode()).hexdigest()
        out[width] = (str(path), size, digest)
    return out
