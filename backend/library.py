"""SUN library tabs backend: PDF 预览 + 教材下载.

One immutable catalog release (`sun-catalog-v1`) is loaded from
``${DATA_ROOT}/catalog/CURRENT`` -> ``${DATA_ROOT}/catalog/releases/<id>/``
and shared by every consumer in this module (search-hit resolution, preview,
downloads).  Nothing here touches the search/chat code paths of main.py.

Design rules carried over from the verified keben fixes:
  * identity only by identifier (edition_id / source_id / book_key), never by title;
  * three page numberings never mixed: ``pdf_page`` (1-based position in the PDF),
    ``printed_label`` (shown only when ``printed_verified``) and
    ``legacy_image_index`` (0-based SUN/JC page-image index);
  * no offset is ever assumed: a legacy page resolves only through a page-map row;
  * no user-supplied path reaches the filesystem: PDF paths are derived from the
    sha256 recorded in the loaded release.

Only FastAPI/Starlette and the standard library are used.
See docs/LIBRARY_TABS.md for the operator documentation.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

SCHEMA = "sun-catalog-v1"

# ── Limits (every request parameter is capped) ───────────────────────────────
MAX_LIST_LIMIT = 60
DEFAULT_EDITIONS_LIMIT = 60
DEFAULT_DOWNLOADS_LIMIT = 30
MAX_OFFSET = 100_000
MAX_Q_CHARS = 80
MAX_FACET_CHARS = 60
MAX_PAGE_SPAN = 200
MAX_RESOLVE_ITEMS = 40
MAX_BOOK_KEY_CHARS = 400
MAX_ITEMS_PARAM_CHARS = 2000
MAX_PAGE_NUMBER = 100_000
MAX_CATALOG_JSON_BYTES = 64 * 1024 * 1024
MAX_PAGE_MAP_BYTES = 4 * 1024 * 1024
MAX_PAGES_PER_BOOK = 5000
STREAM_CHUNK = 64 * 1024

RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
EDITION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,95}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA12_RE = re.compile(r"^[0-9a-f]{12}$")
SHORT_KEY_RE = re.compile(r"^[0-9a-f]{12}$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
STATUSES = ("active", "superseded", "pending-verification")
CORPUS_RELATIONS = ("same-bytes", "aligned", "partial", "different-edition", "none")
SEARCHABLE_RELATIONS = ("same-bytes", "aligned", "partial")
TEXT_IDENTITIES = ("same-file", "page-aligned-other-printing")
STAGE_ORDER = {"小学": 0, "初中": 1, "高中": 2}
SOURCE_LABELS = {"vps": "本站", "drive": "云端硬盘", "upstream": "官方平台"}
SOURCE_ORDER = ("vps", "drive", "upstream")

UNTITLED = "未命名教材"
DRIVE_HOSTS = ("drive.google.com", "drive.usercontent.google.com", "docs.google.com")
DEFAULT_UPSTREAM_HOSTS = "jks-ai.bdfz.net,smartedu.cn,ykt.cbern.com.cn"
DEFAULT_COVER_HOSTS = "jks-ai.bdfz.net,ykt.cbern.com.cn,smartedu.cn,img.rdfzer.com,img.bdfz.net"

mimetypes.add_type("text/javascript", ".mjs")


class ReleaseError(Exception):
    """A catalog release is absent or failed validation."""


# ── Settings ─────────────────────────────────────────────────────────────────

def _env_int(env: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(str(env.get(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def _env_float(env: Mapping[str, str], name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(str(env.get(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def parse_networks(value: Any) -> tuple:
    """`LIBRARY_TRUSTED_PROXIES`: comma-separated addresses or CIDR blocks (same
    syntax as AI_TRUSTED_PROXIES).  Unparseable entries and blocks wider than
    /8 (IPv4) or /16 (IPv6) are ignored: a typo must never trust the world."""
    out = []
    for item in str(value or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError:
            continue
        if network.prefixlen < (8 if network.version == 4 else 16):
            continue
        out.append(network)
    return tuple(out)


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip().lower() for item in str(value or "").split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    catalog_root: Path
    pdf_root: Path
    serve_mode: str            # "accel" (production) | "direct" (dev/tests)
    accel_prefix: str
    reload_seconds: int
    json_capacity: int
    json_per_second: float
    pdf_capacity: int
    pdf_per_second: float
    trusted_proxies: tuple   # ip_network objects; loopback is always trusted in addition
    upstream_hosts: tuple[str, ...]
    cover_hosts: tuple[str, ...]
    upstream_template: str
    preview_policy: str = "catalog"   # "catalog": storage.vps recorded | "present": file found in pdf_root at load (dev)

    @classmethod
    def from_env(cls, data_root: Path, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        mode = str(env.get("PDF_SERVE_MODE", "accel")).strip().lower()
        if mode not in ("accel", "direct"):
            mode = "accel"
        prefix = str(env.get("LIBRARY_ACCEL_PREFIX", "/_protected_pdf/")).strip() or "/_protected_pdf/"
        if not re.fullmatch(r"/[A-Za-z0-9_./-]*", prefix):
            prefix = "/_protected_pdf/"
        if not prefix.endswith("/"):
            prefix += "/"
        template = str(env.get("LIBRARY_UPSTREAM_TEMPLATE", "")).strip()
        return cls(
            catalog_root=Path(env.get("LIBRARY_CATALOG_ROOT") or (Path(data_root) / "catalog")),
            pdf_root=Path(env.get("LIBRARY_PDF_ROOT") or "/srv/sun-textbooks/pdf"),
            serve_mode=mode,
            accel_prefix=prefix,
            reload_seconds=_env_int(env, "LIBRARY_RELOAD_SECONDS", 60, 0, 86400),
            json_capacity=_env_int(env, "LIBRARY_RATE_JSON_BURST", 240, 1, 100000),
            json_per_second=_env_float(env, "LIBRARY_RATE_JSON_PER_SECOND", 8.0, 0.01, 10000.0),
            pdf_capacity=_env_int(env, "LIBRARY_RATE_PDF_BURST", 1200, 1, 100000),
            pdf_per_second=_env_float(env, "LIBRARY_RATE_PDF_PER_SECOND", 40.0, 0.01, 10000.0),
            trusted_proxies=parse_networks(env.get("LIBRARY_TRUSTED_PROXIES", "")),
            upstream_hosts=_csv(env.get("LIBRARY_UPSTREAM_HOSTS", DEFAULT_UPSTREAM_HOSTS)),
            cover_hosts=_csv(env.get("LIBRARY_COVER_HOSTS", DEFAULT_COVER_HOSTS)),
            upstream_template=template,
            preview_policy=("present" if str(env.get("LIBRARY_PREVIEW_POLICY", "catalog")).strip().lower() == "present"
                            else "catalog"),
        )


# ── Small validators ─────────────────────────────────────────────────────────

def _opt_str(value: Any, limit: int = 300) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == item or host.endswith("." + item) for item in allowed)


def safe_https_url(value: Any, allowed_hosts: tuple[str, ...]) -> Optional[str]:
    """Return the URL only if it is https, has no credentials/whitespace and its
    host is on the allow-list."""
    if not isinstance(value, str) or len(value) > 2000:
        return None
    if re.search(r"[\s\"'<>\\`\x00-\x1f\x7f]", value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    try:
        if parts.port not in (None, 443):
            return None
    except ValueError:
        return None
    if not _host_allowed(parts.hostname, allowed_hosts):
        return None
    return value


def derive_filename(title: Optional[str], edition_label: Optional[str], edition_id: str) -> str:
    """Server-side download filename. Never taken from the request."""
    base = (title or "").strip()
    if edition_label and edition_label.strip() and edition_label.strip() not in base:
        base = f"{base}（{edition_label.strip()}）" if base else edition_label.strip()
    base = re.sub(r"[\x00-\x1f\x7f/\\:*?\"<>|;%#&]+", " ", base)
    base = re.sub(r"\s+", " ", base).strip(" .")
    if not base:
        base = edition_id
    return base[:80].rstrip(" .") + ".pdf"


def short_key_for(book_key: str) -> str:
    """keben-verified rule: shortKey = md5(book_key)[:12] (144/144 keys)."""
    return hashlib.md5(book_key.encode("utf-8")).hexdigest()[:12]


# ── Release model ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PageMap:
    version: Optional[str]
    rows: tuple[tuple[Optional[str], bool, Optional[int], Optional[int], Optional[int]], ...]
    # row = (printed_label, printed_verified, legacy_image_index, ocr_page, jks_page); index = pdf_page-1
    by_legacy: Mapping[int, int]
    by_jks: Mapping[int, int]
    printed_verified_pages: int
    legacy_mapped_pages: int

    def row(self, pdf_page: int) -> dict:
        printed, verified, legacy, ocr, jks = self.rows[pdf_page - 1]
        out = {
            "pdf_page": pdf_page,
            # A printed number is exposed only when it is verified.
            "printed_label": printed if verified else None,
            "printed_verified": bool(verified),
            "legacy_image_index": legacy,
            "ocr_page": ocr,
        }
        return out


@dataclass(frozen=True)
class Edition:
    edition_id: str
    record: Mapping[str, Any]          # sanitised public record
    stage: Optional[str]
    subject: Optional[str]
    title: str
    region: str
    status: str
    sha256: str
    pages: int
    size: int
    has_vps: bool
    filename: str
    sun_book_key: Optional[str]
    jc_short_key: Optional[str]
    jks_slug: Optional[str]
    jks_pages_are_pdf_pages: bool
    corpus_relation: str
    page_map: Optional[PageMap]
    haystack: str
    order: int

    @property
    def sha12(self) -> str:
        return self.sha256[:12]

    @property
    def previewable(self) -> bool:
        return self.has_vps

    @property
    def searchable(self) -> bool:
        return bool(self.sun_book_key) and self.corpus_relation in SEARCHABLE_RELATIONS

    def file_url(self, download: bool = False) -> str:
        url = f"/files/pdf/{self.edition_id}/{self.sha12}/{quote(self.filename, safe='')}"
        return url + ("?download=1" if download else "")


@dataclass(frozen=True)
class DownloadRow:
    record: Mapping[str, Any]
    stage: Optional[str]
    subject: Optional[str]
    publisher: Optional[str]
    region: str
    haystack: str


@dataclass(frozen=True)
class Release:
    release_id: str
    generated_at: Optional[str]
    editions: Mapping[str, Edition]
    edition_order: tuple[str, ...]
    downloads: tuple[DownloadRow, ...]
    by_sun_key: Mapping[str, tuple[str, ...]]
    by_short_key: Mapping[str, tuple[str, ...]]
    by_jks_slug: Mapping[str, tuple[str, ...]]
    summary: Mapping[str, Any]
    loaded_at: float = field(default_factory=time.time)


def _read_json(path: Path, limit: int) -> Any:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ReleaseError(f"missing file: {path.name}") from exc
    if size > limit:
        raise ReleaseError(f"file too large: {path.name}")
    try:
        with path.open("rb") as handle:
            return json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ReleaseError(f"invalid JSON: {path.name}") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_sums(release_dir: Path) -> set[str]:
    sums_path = release_dir / "SHA256SUMS"
    try:
        text = sums_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReleaseError("missing file: SHA256SUMS") from exc
    listed: set[str] = set()
    root = release_dir.resolve()
    for line in text.splitlines():
        line = line.rstrip("\r")
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-fA-F]{64})[ \t]+\*?(.+)", line)
        if not match:
            raise ReleaseError("malformed SHA256SUMS line")
        rel = match.group(2).strip()
        if rel.startswith("./"):
            rel = rel[2:]
        rel_path = Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts or "\\" in rel:
            raise ReleaseError("unsafe path in SHA256SUMS")
        target = (release_dir / rel_path)
        try:
            resolved = target.resolve()
        except OSError as exc:
            raise ReleaseError(f"unreadable file: {rel}") from exc
        if root not in resolved.parents or not resolved.is_file():
            raise ReleaseError(f"missing file listed in SHA256SUMS: {rel}")
        if _sha256_file(resolved) != match.group(1).lower():
            raise ReleaseError(f"sha256 mismatch: {rel}")
        listed.add(rel_path.as_posix())
    for required in ("catalog.json", "downloads.json", "RELEASE.json"):
        if required not in listed:
            raise ReleaseError(f"SHA256SUMS does not cover {required}")
    return listed


def _parse_page_map(raw: Any, edition_id: str, pages: int, sha256: str) -> PageMap:
    version = None
    rows_raw = raw
    if isinstance(raw, dict):
        if raw.get("edition_id") not in (None, edition_id):
            raise ReleaseError(f"page map edition mismatch: {edition_id}")
        bound = raw.get("pdf_sha256") or raw.get("sha256")
        if bound is not None and bound != sha256:
            raise ReleaseError(f"page map bound to another PDF: {edition_id}")
        version = _opt_str(raw.get("page_map_version") or raw.get("version"), 80)
        rows_raw = raw.get("pages")
    if not isinstance(rows_raw, list) or len(rows_raw) != pages:
        raise ReleaseError(f"page map length does not equal PDF page count: {edition_id}")
    rows = []
    by_legacy: dict[int, int] = {}
    by_jks: dict[int, int] = {}
    verified_count = 0
    for position, item in enumerate(rows_raw, start=1):
        if not isinstance(item, dict) or item.get("pdf_page") != position:
            raise ReleaseError(f"page map rows must be contiguous from 1: {edition_id}")
        printed = _opt_str(item.get("printed_label"), 24)
        verified = item.get("printed_verified", False)
        if not isinstance(verified, bool):
            raise ReleaseError(f"printed_verified must be boolean: {edition_id}")
        if verified and printed is None:
            raise ReleaseError(f"verified page without printed_label: {edition_id}")
        legacy = item.get("legacy_image_index")
        if legacy is not None:
            if not _is_int(legacy) or legacy < 0 or legacy > MAX_PAGE_NUMBER:
                raise ReleaseError(f"bad legacy_image_index: {edition_id}")
            if legacy in by_legacy:
                raise ReleaseError(f"duplicate legacy_image_index: {edition_id}")
            by_legacy[legacy] = position
        ocr = item.get("ocr_page")
        if ocr is not None and (not _is_int(ocr) or ocr < 0 or ocr > MAX_PAGE_NUMBER):
            raise ReleaseError(f"bad ocr_page: {edition_id}")
        jks = item.get("jks_page")
        if jks is not None:
            if not _is_int(jks) or jks < 1 or jks > MAX_PAGE_NUMBER or jks in by_jks:
                raise ReleaseError(f"bad jks_page: {edition_id}")
            by_jks[jks] = position
        if verified:
            verified_count += 1
        rows.append((printed, verified, legacy, ocr, jks))
    return PageMap(
        version=version,
        rows=tuple(rows),
        by_legacy=by_legacy,
        by_jks=by_jks,
        printed_verified_pages=verified_count,
        legacy_mapped_pages=len(by_legacy),
    )


def _drive_source(raw: Any) -> Optional[dict]:
    """Drive is offered only when the catalog records a shared copy."""
    if not isinstance(raw, dict) or raw.get("shared") is not True:
        return None
    download_url = safe_https_url(raw.get("download_url"), DRIVE_HOSTS)
    view_url = safe_https_url(raw.get("view_url"), DRIVE_HOSTS)
    url = download_url or view_url
    if not url:
        return None
    return {"kind": "drive", "label": SOURCE_LABELS["drive"], "url": url,
            "view_url": view_url, "same_origin": False, "available": True}


def _build_edition(raw: Any, order: int, release_dir: Path, listed: set[str], settings: Settings) -> Edition:
    if not isinstance(raw, dict):
        raise ReleaseError("edition record is not an object")
    edition_id = raw.get("edition_id")
    if not isinstance(edition_id, str) or not EDITION_ID_RE.fullmatch(edition_id):
        raise ReleaseError("invalid edition_id")
    title = _opt_str(raw.get("title"), 200) or UNTITLED
    status = raw.get("status")
    if status not in STATUSES:
        raise ReleaseError(f"invalid status: {edition_id}")
    pdf = raw.get("pdf")
    has_pdf = pdf is not None
    if has_pdf:
        if not isinstance(pdf, dict):
            raise ReleaseError(f"invalid pdf block: {edition_id}")
        sha256 = pdf.get("sha256")
        pages = pdf.get("pages")
        size = pdf.get("bytes")
        if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
            raise ReleaseError(f"invalid pdf.sha256: {edition_id}")
        if not _is_int(pages) or not (1 <= pages <= MAX_PAGES_PER_BOOK):
            raise ReleaseError(f"invalid pdf.pages: {edition_id}")
        if not _is_int(size) or size <= 0:
            raise ReleaseError(f"invalid pdf.bytes: {edition_id}")
    else:
        # An edition that is catalogued but not acquired yet: never previewable.
        if status == "active":
            raise ReleaseError(f"active edition without a PDF: {edition_id}")
        pdf, sha256, pages, size = {}, "", 0, 0
    storage = raw.get("storage") if isinstance(raw.get("storage"), dict) else {}
    has_vps = has_pdf and isinstance(storage.get("vps"), dict)
    if has_pdf and not has_vps and settings.preview_policy == "present":
        has_vps = (settings.pdf_root / f"{sha256}.pdf").is_file()
    drive = _drive_source(storage.get("drive"))
    upstream = storage.get("upstream") if isinstance(storage.get("upstream"), dict) else {}
    legacy = raw.get("legacy") if isinstance(raw.get("legacy"), dict) else {}
    sun_book_key = legacy.get("sun_book_key")
    if sun_book_key is not None and (not isinstance(sun_book_key, str) or not (0 < len(sun_book_key) <= MAX_BOOK_KEY_CHARS)):
        raise ReleaseError(f"invalid legacy.sun_book_key: {edition_id}")
    jc_short_key = legacy.get("jc_short_key")
    if jc_short_key is not None and (not isinstance(jc_short_key, str) or not SHORT_KEY_RE.fullmatch(jc_short_key)):
        raise ReleaseError(f"invalid legacy.jc_short_key: {edition_id}")
    jks_slug = legacy.get("jks_slug")
    if jks_slug is not None and (not isinstance(jks_slug, str) or not SLUG_RE.fullmatch(jks_slug)):
        raise ReleaseError(f"invalid legacy.jks_slug: {edition_id}")
    corpus = raw.get("corpus") if isinstance(raw.get("corpus"), dict) else {}
    relation = corpus.get("relation") or "none"
    if relation not in CORPUS_RELATIONS:
        raise ReleaseError(f"invalid corpus.relation: {edition_id}")
    source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    processing = raw.get("processing") if isinstance(raw.get("processing"), dict) else {}
    platform_current = raw.get("platform_current", source.get("platform_current"))
    if not isinstance(platform_current, bool):
        platform_current = None
    # Is the searchable corpus text the text of THIS file?  When the release does not
    # say, assume the weaker claim for anything that is not byte-identical.
    declared_identity = corpus.get("text_identity")
    if declared_identity in TEXT_IDENTITIES:
        text_identity = declared_identity
    elif relation == "same-bytes":
        text_identity = "same-file"
    elif relation in ("aligned", "partial"):
        text_identity = "page-aligned-other-printing"
    else:
        text_identity = None

    page_map = None
    rel = f"page-maps/{edition_id}.json"
    map_path = release_dir / "page-maps" / f"{edition_id}.json"
    if has_pdf and map_path.is_file():
        if rel not in listed:
            raise ReleaseError(f"page map not covered by SHA256SUMS: {edition_id}")
        page_map = _parse_page_map(_read_json(map_path, MAX_PAGE_MAP_BYTES), edition_id, pages, sha256)

    stage = _opt_str(raw.get("stage"), 20)
    subject = _opt_str(raw.get("subject"), 40)
    publisher = _opt_str(raw.get("publisher"), 80)
    edition_label = _opt_str(raw.get("edition_label"), 80)
    volume = _opt_str(raw.get("volume"), 80)
    region = _opt_str(raw.get("region"), 8) or "CN"
    filename = derive_filename(title, edition_label, edition_id)
    record = {
        "edition_id": edition_id,
        "work_id": _opt_str(raw.get("work_id"), 200),
        "region": region,
        "stage": stage,
        "subject": subject,
        "title": title,
        "volume": volume,
        "publisher": publisher,
        "edition_label": edition_label,
        "status": status,
        "source": {
            "provider": _opt_str(source.get("provider"), 40),
            "source_id": _opt_str(source.get("source_id"), 96),
            "online_time": _opt_str(source.get("online_time"), 40),
            "update_time": _opt_str(source.get("update_time"), 40),
            "retrieved_at": _opt_str(source.get("retrieved_at"), 40),
            "id_basis": _opt_str(source.get("id_basis", source.get("source_id_basis")), 80),
        },
        "metadata_basis": _opt_str(raw.get("metadata_basis"), 80),
        # Whether this file is the one the official platform serves now.  Unknown stays null.
        "platform_current": platform_current,
        # Filled in by load_release once every edition is known (dangling ids become null).
        "newer_edition_id": _edition_ref(raw.get("newer_edition_id")),
        "supersedes_edition_id": _edition_ref(raw.get("supersedes_edition_id")),
        "pdf": ({
            "sha256": sha256,
            "bytes": size,
            "pages": pages,
            "has_text_layer": pdf.get("has_text_layer") if isinstance(pdf.get("has_text_layer"), bool) else None,
        } if has_pdf else None),
        "corpus": {
            "relation": relation,
            "text_identity": text_identity,
            "text_identity_declared": declared_identity in TEXT_IDENTITIES,
            "basis": _opt_str(corpus.get("basis"), 200),
            "coverage": corpus.get("coverage") if isinstance(corpus.get("coverage"), (int, float)) and not isinstance(corpus.get("coverage"), bool) else _opt_str(corpus.get("coverage"), 80),
        },
        "processing": {
            "ocr_version": _opt_str(processing.get("ocr_version"), 80),
            "index_version": _opt_str(processing.get("index_version"), 80),
            "page_map_version": _opt_str(processing.get("page_map_version"), 80),
        },
        "legacy": {"sun_book_key": sun_book_key, "jc_short_key": jc_short_key, "jks_slug": jks_slug},
        "storage": {
            "vps": has_vps,
            "drive": drive,
            "upstream_available": upstream.get("available") is True,
        },
    }
    haystack = " ".join(filter(None, [title, volume, publisher, edition_label, subject, stage])).lower()
    return Edition(
        edition_id=edition_id, record=record, stage=stage, subject=subject, title=title,
        region=region, status=status, sha256=sha256, pages=pages, size=size, has_vps=has_vps,
        filename=filename, sun_book_key=sun_book_key, jc_short_key=jc_short_key, jks_slug=jks_slug,
        jks_pages_are_pdf_pages=legacy.get("jks_pages") == "pdf_page",
        corpus_relation=relation, page_map=page_map, haystack=haystack, order=order,
    )


def _build_download(raw: Any, editions: Mapping[str, Edition], settings: Settings, dropped: list[int]) -> DownloadRow:
    if not isinstance(raw, dict):
        raise ReleaseError("download record is not an object")
    title = _opt_str(raw.get("title"), 200) or UNTITLED
    source_id = _opt_str(raw.get("source_id"), 96)
    if source_id is not None and not SOURCE_ID_RE.fullmatch(source_id):
        raise ReleaseError("invalid download source_id")
    edition_id = raw.get("edition_id")
    edition: Optional[Edition] = None
    if edition_id is not None:
        if not isinstance(edition_id, str) or edition_id not in editions:
            raise ReleaseError(f"download row points to unknown edition: {str(edition_id)[:96]}")
        edition = editions[edition_id]
    size = raw.get("size_bytes")
    if not _is_int(size) or size <= 0:
        size = None

    by_kind: dict[str, dict] = {}
    if edition is not None and edition.has_vps:
        # The URL is always derived from the loaded release, never from the row.
        by_kind["vps"] = {"kind": "vps", "label": SOURCE_LABELS["vps"], "url": edition.file_url(download=True),
                          "same_origin": True, "available": True, "bytes": edition.size}
        if size is None:
            size = edition.size
    raw_sources = raw.get("sources") if isinstance(raw.get("sources"), list) else []
    for item in raw_sources:
        if isinstance(item, str):
            item = {"kind": item}
        if not isinstance(item, dict):
            dropped[0] += 1
            continue
        kind = item.get("kind")
        if kind == "vps":
            continue  # only the derived entry above is ever offered
        if kind == "drive":
            drive = _drive_source({**item, "shared": item.get("shared", False)})
            if drive is None and edition is not None:
                drive = edition.record["storage"]["drive"]
            if drive is None:
                dropped[0] += 1
            else:
                by_kind.setdefault("drive", dict(drive))
        elif kind == "upstream":
            url = safe_https_url(item.get("url"), settings.upstream_hosts)
            if url is None and item.get("url") is None and settings.upstream_template and source_id:
                url = safe_https_url(
                    settings.upstream_template.replace("{source_id}", quote(source_id, safe="")),
                    settings.upstream_hosts,
                )
            if url is None:
                dropped[0] += 1
            else:
                by_kind.setdefault("upstream", {"kind": "upstream", "label": SOURCE_LABELS["upstream"], "url": url,
                                                 "same_origin": False, "available": item.get("available", True) is not False})
        else:
            dropped[0] += 1
    if "drive" not in by_kind and edition is not None and edition.record["storage"]["drive"]:
        by_kind["drive"] = dict(edition.record["storage"]["drive"])

    stage = _opt_str(raw.get("stage"), 20)
    subject = _opt_str(raw.get("subject"), 40)
    publisher = _opt_str(raw.get("publisher"), 80)
    edition_label = _opt_str(raw.get("edition_label"), 80)
    region = _opt_str(raw.get("region"), 8) or (edition.region if edition else "CN")
    record = {
        "source_id": source_id,
        "region": region,
        "stage": stage,
        "subject": subject,
        "title": title,
        "edition_label": edition_label,
        "publisher": publisher,
        "cover": safe_https_url(raw.get("cover"), settings.cover_hosts),
        "size_bytes": size,
        "edition_id": edition.edition_id if edition else None,
        "row_id": edition.edition_id if edition else (source_id or "t-" + hashlib.sha256(
            "\x1f".join([title, stage or "", subject or "", edition_label or "", publisher or ""]).encode("utf-8")).hexdigest()[:16]),
        "previewable": bool(edition and edition.previewable),
        "platform_current": edition.record["platform_current"] if edition else None,
        "retrieved_at": edition.record["source"]["retrieved_at"] if edition else None,
        "newer_edition": None,   # filled by load_release
        "sources": [by_kind[kind] for kind in SOURCE_ORDER if kind in by_kind],
        # Other files held for the same source id are different editions: listed, never merged.
        "other_editions": [
            {"edition_id": other.edition_id, "edition_label": other.record["edition_label"],
             "status": other.status, "previewable": other.previewable,
             "platform_current": other.record["platform_current"],
             "retrieved_at": other.record["source"]["retrieved_at"]}
            for other in (editions.get(i) for i in (raw.get("held_other_edition_ids") or [])[:8] if isinstance(i, str))
            if other is not None and (edition is None or other.edition_id != edition.edition_id)
        ],
    }
    haystack = " ".join(filter(None, [title, edition_label, publisher, subject, stage])).lower()
    return DownloadRow(record=record, stage=stage, subject=subject, publisher=publisher, region=region, haystack=haystack)


def _edition_ref(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and EDITION_ID_RE.fullmatch(value) else None


def _contract(doc: Mapping[str, Any]) -> Optional[str]:
    """The generator names the contract in `contract`; `schema` is accepted too."""
    value = doc.get("contract", doc.get("schema"))
    return value if value is None or isinstance(value, str) else "invalid"


def _index(pairs: list[tuple[str, str]]) -> dict[str, tuple[str, ...]]:
    out: dict[str, list[str]] = {}
    for key, edition_id in pairs:
        bucket = out.setdefault(key, [])
        if edition_id not in bucket:
            bucket.append(edition_id)
    return {key: tuple(value) for key, value in out.items()}


def load_release(catalog_root: Path, settings: Settings, release_id: Optional[str] = None) -> Release:
    """Read, verify and freeze one release. Raises ReleaseError on any problem."""
    catalog_root = Path(catalog_root)
    if release_id is None:
        release_id = read_current_id(catalog_root)
    if not RELEASE_ID_RE.fullmatch(release_id) or release_id in (".", ".."):
        raise ReleaseError("invalid release id")
    release_dir = catalog_root / "releases" / release_id
    if not release_dir.is_dir():
        raise ReleaseError("release directory not found")
    listed = _verify_sums(release_dir)

    meta = _read_json(release_dir / "RELEASE.json", MAX_PAGE_MAP_BYTES)
    if not isinstance(meta, dict) or meta.get("release_id") != release_id:
        raise ReleaseError("RELEASE.json release_id does not match directory")
    if _contract(meta) not in (None, SCHEMA):
        raise ReleaseError("unsupported catalog schema")

    catalog = _read_json(release_dir / "catalog.json", MAX_CATALOG_JSON_BYTES)
    if isinstance(catalog, dict):
        if _contract(catalog) not in (None, SCHEMA):
            raise ReleaseError("unsupported catalog schema")
        if catalog.get("release_id") not in (None, release_id):
            raise ReleaseError("catalog.json belongs to another release")
        catalog = catalog.get("editions")
    if not isinstance(catalog, list):
        raise ReleaseError("catalog.json has no editions list")

    editions: dict[str, Edition] = {}
    for order, raw in enumerate(catalog):
        edition = _build_edition(raw, order, release_dir, listed, settings)
        if edition.edition_id in editions:
            raise ReleaseError(f"duplicate edition_id: {edition.edition_id}")
        editions[edition.edition_id] = edition

    for edition in editions.values():
        for key in ("newer_edition_id", "supersedes_edition_id"):
            ref = edition.record[key]
            if ref is not None and (ref not in editions or ref == edition.edition_id):
                edition.record[key] = None

    downloads_raw = _read_json(release_dir / "downloads.json", MAX_CATALOG_JSON_BYTES)
    if isinstance(downloads_raw, dict):
        if downloads_raw.get("release_id") not in (None, release_id):
            raise ReleaseError("downloads.json belongs to another release")
        if _contract(downloads_raw) not in (None, SCHEMA, SCHEMA + "/downloads"):
            raise ReleaseError("unsupported catalog schema")
        # the generator writes "rows"; "downloads" is accepted as an alias
        downloads_raw = downloads_raw.get("rows") if "rows" in downloads_raw else downloads_raw.get("downloads")
    if not isinstance(downloads_raw, list):
        raise ReleaseError("downloads.json has no rows list")
    dropped = [0]
    downloads = [_build_download(raw, editions, settings, dropped) for raw in downloads_raw]
    # A file held on this site whose edition no row points at (e.g. the platform now
    # serves a different file under the same id) is listed as its own row: different
    # editions are never merged into one entry.
    referenced = {row.record["edition_id"] for row in downloads if row.record["edition_id"]}
    for edition in editions.values():
        if edition.has_vps and edition.edition_id not in referenced:
            downloads.append(_build_download({
                "source_id": edition.record["source"]["source_id"], "stage": edition.stage, "subject": edition.subject,
                "title": edition.title, "edition_label": edition.record["edition_label"],
                "publisher": edition.record["publisher"], "cover": None, "size_bytes": edition.size,
                "sources": [], "edition_id": edition.edition_id, "region": edition.region,
            }, editions, settings, dropped))
    seen_rows: set[str] = set()
    for row in downloads:
        if row.record["row_id"] in seen_rows:
            raise ReleaseError(f"duplicate download row: {row.record['row_id']}")
        seen_rows.add(row.record["row_id"])
    for row in downloads:
        held = editions.get(row.record["edition_id"]) if row.record["edition_id"] else None
        if held is not None and held.record["newer_edition_id"]:
            row.record["newer_edition"] = _edition_brief(editions, downloads, held.record["newer_edition_id"])
    # stable sort; the title key puts a file held on this site next to the platform's
    # entry for the same book (platform entry first)
    downloads.sort(key=lambda row: (STAGE_ORDER.get(row.stage or "", 9), row.subject or "", row.record["title"],
                                    row.record["platform_current"] is False))

    sun_pairs, short_pairs, jks_pairs = [], [], []
    for edition in editions.values():
        if edition.sun_book_key:
            sun_pairs.append((edition.sun_book_key, edition.edition_id))
            short_pairs.append((short_key_for(edition.sun_book_key), edition.edition_id))
        if edition.jc_short_key:
            short_pairs.append((edition.jc_short_key, edition.edition_id))
        if edition.jks_slug:
            jks_pairs.append((edition.jks_slug, edition.edition_id))

    previewable = [e for e in editions.values() if e.previewable]
    by_stage: dict[str, int] = {}
    for edition in previewable:
        if edition.status != "active":
            continue  # the grid lists active editions only
        by_stage[edition.stage or "未标注"] = by_stage.get(edition.stage or "未标注", 0) + 1
    summary = {
        "counts": {
            "editions": len(editions),
            "previewable": len(previewable),
            "active_previewable": sum(1 for e in previewable if e.status == "active"),
            "downloads": len(downloads),
            "downloads_on_site": sum(1 for row in downloads if any(s["kind"] == "vps" for s in row.record["sources"])),
            "downloads_on_drive": sum(1 for row in downloads if any(s["kind"] == "drive" for s in row.record["sources"])),
            "downloads_without_source": sum(1 for row in downloads if not row.record["sources"]),
            "page_maps": sum(1 for e in editions.values() if e.page_map is not None),
            "dropped_sources": dropped[0],
        },
        "coverage": {
            "printed_verified_editions": sum(1 for e in previewable if e.page_map and e.page_map.printed_verified_pages > 0),
            "legacy_mapped_editions": sum(1 for e in previewable if e.page_map and e.page_map.legacy_mapped_pages > 0),
            "searchable_editions": sum(1 for e in previewable if e.searchable),
            "text_layer_editions": sum(1 for e in previewable if (e.record["pdf"] or {}).get("has_text_layer") is True),
            "previewable_by_stage": by_stage,
        },
        "stages": sorted({row.stage for row in downloads if row.stage} | {e.stage for e in editions.values() if e.stage},
                         key=lambda s: (STAGE_ORDER.get(s, 9), s)),
        "regions": sorted({row.region for row in downloads} | {e.region for e in editions.values()}),
    }
    return Release(
        release_id=release_id,
        generated_at=_opt_str(meta.get("generated_at"), 40),
        editions=editions,
        edition_order=tuple(editions.keys()),
        downloads=tuple(downloads),
        by_sun_key=_index(sun_pairs),
        by_short_key=_index(short_pairs),
        by_jks_slug=_index(jks_pairs),
        summary=summary,
    )


def read_current_id(catalog_root: Path) -> str:
    try:
        with (Path(catalog_root) / "CURRENT").open("r", encoding="utf-8") as handle:
            text = handle.read(256)
    except (OSError, UnicodeDecodeError) as exc:
        raise ReleaseError("no CURRENT pointer") from exc
    release_id = text.strip()
    if "\n" in release_id or not RELEASE_ID_RE.fullmatch(release_id):
        raise ReleaseError("invalid CURRENT pointer")
    return release_id


class LibraryState:
    """Holds the single loaded release.

    ``get()`` returns the same frozen ``Release`` object to every consumer.  The
    CURRENT pointer is re-read at most once per ``reload_seconds``; a new id is
    loaded and verified completely before the reference is swapped in one
    assignment, so a request never observes a half-loaded or mixed release.  If
    the new release is invalid the previous one keeps serving and the error is
    reported by /api/library/release.
    """

    def __init__(self, settings: Settings, clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self._clock = clock
        self._lock = threading.Lock()
        self._release: Optional[Release] = None
        self._error: Optional[str] = "catalog not loaded"
        self._checked_at: Optional[float] = None

    def get(self) -> Optional[Release]:
        now = self._clock()
        checked = self._checked_at
        due = checked is None or (self.settings.reload_seconds > 0 and now - checked >= self.settings.reload_seconds)
        if due:
            with self._lock:
                checked = self._checked_at
                if checked is None or (self.settings.reload_seconds > 0 and now - checked >= self.settings.reload_seconds):
                    self._refresh()
                    self._checked_at = now
        return self._release

    def reload_now(self) -> Optional[Release]:
        with self._lock:
            self._refresh()
            self._checked_at = self._clock()
        return self._release

    def _refresh(self) -> None:
        try:
            wanted = read_current_id(self.settings.catalog_root)
            if self._release is not None and self._release.release_id == wanted:
                self._error = None
                return
            release = load_release(self.settings.catalog_root, self.settings, wanted)
        except ReleaseError as exc:
            self._error = str(exc)
            return
        except Exception as exc:  # never let a catalog problem escape into SUN
            self._error = f"unexpected catalog error: {type(exc).__name__}"
            return
        self._release = release
        self._error = None

    @property
    def error(self) -> Optional[str]:
        return self._error


# ── Rate limiting ────────────────────────────────────────────────────────────

class RateLimiter:
    """In-process token bucket per client key."""

    def __init__(self, capacity: int, per_second: float, clock: Callable[[], float] = time.monotonic, max_keys: int = 20000):
        self.capacity = float(capacity)
        self.per_second = float(per_second)
        self._clock = clock
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}

    def take(self, key: str, cost: float = 1.0) -> tuple[bool, int]:
        now = self._clock()
        with self._lock:
            # pop + re-insert keeps the dict ordered by last use (oldest first)
            tokens, stamp = self._buckets.pop(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + max(0.0, now - stamp) * self.per_second)
            if tokens >= cost:
                self._buckets[key] = (tokens - cost, now)
                allowed, retry = True, 0
            else:
                self._buckets[key] = (tokens, now)
                allowed, retry = False, max(1, int((cost - tokens) / self.per_second + 0.999))
            # Bounded table: drop the least recently used keys, never the whole map,
            # so a flood of fresh keys cannot refill an exhausted bucket that is still in use.
            while len(self._buckets) > self._max_keys:
                del self._buckets[next(iter(self._buckets))]
        return allowed, retry


def _parse_ip(value: Optional[str]):
    if not value or len(value) > 64:
        return None
    try:
        return ipaddress.ip_address(value.strip().split("%", 1)[0])
    except ValueError:
        return None


def client_key(request: Request, trusted_proxies: tuple = ()) -> str:
    """Rate-limit key.  ``X-Real-IP`` is honoured only when the TCP peer is
    loopback or inside an explicitly configured proxy network; IPv6 is grouped by /64."""
    peer_text = request.client.host if request.client else ""
    peer = _parse_ip(peer_text)
    address = peer
    if peer is not None and peer.version == 6 and peer.ipv4_mapped is not None:
        peer = peer.ipv4_mapped
    trusted = bool(peer and (peer.is_loopback or any(
        peer.version == network.version and peer in network for network in trusted_proxies)))
    if trusted:
        forwarded = _parse_ip(request.headers.get("x-real-ip"))
        if forwarded is not None:
            address = forwarded
    if address is None:
        return "peer:" + (peer_text[:64] or "unknown")
    if address.version == 6:
        mapped = address.ipv4_mapped
        if mapped is not None:
            return "ip4:" + str(mapped)
        return "ip6:" + str(ipaddress.IPv6Address(int(address) >> 64 << 64)) + "/64"
    return "ip4:" + str(address)


# ── Query helpers ────────────────────────────────────────────────────────────

def _bad(detail: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


def _text_param(value: Optional[str], limit: int, name: str) -> Optional[str]:
    if value is None:
        return None
    if len(value) > limit:
        raise _bad(f"{name} too long")
    value = value.strip()
    return value or None


def _int_param(value: Optional[str], name: str, default: int, lo: int, hi: int) -> int:
    if value is None or value == "":
        return default
    if not re.fullmatch(r"[0-9]{1,7}", value):
        raise _bad(f"{name} must be a non-negative integer")
    number = int(value)
    if number < lo or number > hi:
        raise _bad(f"{name} out of range ({lo}-{hi})")
    return number


def _terms(q: Optional[str]) -> list[str]:
    return [t for t in (q or "").lower().split() if t][:8]


def _matches(haystack: str, terms: list[str]) -> bool:
    return all(term in haystack for term in terms)


def _counts(values) -> list[dict]:
    counts: dict[str, int] = {}
    for value in values:
        if value:
            counts[value] = counts.get(value, 0) + 1
    return [{"name": name, "count": count} for name, count in counts.items()]


def _edition_card(edition: Edition) -> dict:
    record = edition.record
    page_map = edition.page_map
    return {
        "edition_id": edition.edition_id,
        "work_id": record["work_id"],
        "region": edition.region,
        "stage": edition.stage,
        "subject": edition.subject,
        "title": edition.title,
        "volume": record["volume"],
        "publisher": record["publisher"],
        "edition_label": record["edition_label"],
        "status": edition.status,
        "pages": edition.pages,
        "bytes": edition.size,
        "platform_current": record["platform_current"],
        "newer_edition_id": record["newer_edition_id"],
        "badges": {
            "searchable": edition.searchable and bool(page_map and page_map.legacy_mapped_pages > 0),
            "corpus_relation": edition.corpus_relation,
            "printed_verified": bool(page_map and page_map.printed_verified_pages > 0),
            "has_text_layer": (record["pdf"] or {}).get("has_text_layer"),
            "text_identity": record["corpus"]["text_identity"],
        },
    }


def _edition_brief(editions: Mapping[str, Edition], downloads, edition_id: Optional[str]) -> Optional[dict]:
    """A different edition, described on its own terms (never merged with the caller's)."""
    other = editions.get(edition_id) if edition_id else None
    if other is None:
        return None
    source_id = other.record["source"]["source_id"]
    row_id = None
    for row in downloads:
        if row.record["edition_id"] == other.edition_id or (
                row.record["edition_id"] is None and source_id and row.record["source_id"] == source_id):
            row_id = row.record["row_id"]
            break
    return {
        "edition_id": other.edition_id, "title": other.title, "edition_label": other.record["edition_label"],
        "publisher": other.record["publisher"], "status": other.status, "pages": other.pages or None,
        "previewable": other.previewable, "platform_current": other.record["platform_current"],
        "update_time": other.record["source"]["update_time"], "download_row_id": row_id,
    }


def _parse_range(header: Optional[str], size: int):
    """Returns None (serve full body), (start, end) inclusive, or "invalid" (416).
    Multi-range requests are answered with the full body, which RFC 9110 allows."""
    if not header:
        return None
    text = header.strip().lower()
    if not text.startswith("bytes=") or "," in text:
        return None
    match = re.fullmatch(r"bytes=[ \t]*([0-9]*)[ \t]*-[ \t]*([0-9]*)", text)
    if not match:
        return "invalid"
    first, last = match.group(1), match.group(2)
    if not first and not last:
        return "invalid"
    if len(first) > 15 or len(last) > 15:
        return "invalid"
    if not first:
        length = int(last)
        if length == 0:
            return "invalid"
        return (max(0, size - length), size - 1)
    start = int(first)
    if start >= size:
        return "invalid"
    end = min(int(last), size - 1) if last else size - 1
    if end < start:
        return "invalid"
    return (start, end)


def _open_pdf(path: Path):
    return path.open("rb")


async def _file_stream(path: Path, start: int, length: int):
    """Async body iterator.  The file is opened only once iteration starts and
    is closed in ``finally``: when the client disconnects the response task is
    cancelled and the generator is closed, so no descriptor waits for a later
    GC pass (and a response that is never iterated opens nothing)."""
    handle = None
    try:
        handle = await run_in_threadpool(_open_pdf, path)
        await run_in_threadpool(handle.seek, start)
        remaining = length
        while remaining > 0:
            block = await run_in_threadpool(handle.read, min(STREAM_CHUNK, remaining))
            if not block:
                break
            remaining -= len(block)
            yield block
    finally:
        if handle is not None:
            handle.close()


def _etag_matches(header: Optional[str], etag: str) -> bool:
    if not header:
        return False
    if header.strip() == "*":
        return True
    return any(part.strip() in (etag, "W/" + etag) for part in header[:2000].split(","))


# ── Router ───────────────────────────────────────────────────────────────────

class Library:
    def __init__(self, settings: Settings, clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.state = LibraryState(settings, clock)
        self.json_limiter = RateLimiter(settings.json_capacity, settings.json_per_second, clock)
        self.pdf_limiter = RateLimiter(settings.pdf_capacity, settings.pdf_per_second, clock)
        self.router = self._build_router()

    # -- shared guards -------------------------------------------------------
    def _limit(self, request: Request, limiter: RateLimiter) -> None:
        allowed, retry = limiter.take(client_key(request, self.settings.trusted_proxies))
        if not allowed:
            raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试", headers={"Retry-After": str(retry)})

    def _release(self) -> Release:
        release = self.state.get()
        if release is None:
            raise HTTPException(status_code=503, detail="目录尚未发布或不可用", headers={"Retry-After": "60"})
        return release

    @staticmethod
    def _json(payload: dict, release: Optional[Release]) -> JSONResponse:
        headers = {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"}
        if release is not None:
            headers["X-Library-Release"] = release.release_id
            payload = {"release_id": release.release_id, **payload}
        return JSONResponse(payload, headers=headers)

    @staticmethod
    def _edition(release: Release, edition_id: str) -> Edition:
        if not EDITION_ID_RE.fullmatch(edition_id or ""):
            raise _bad("unknown edition", 404)
        edition = release.editions.get(edition_id)
        if edition is None:
            raise _bad("unknown edition", 404)
        return edition

    # -- legacy resolution (never guesses) -----------------------------------
    @staticmethod
    def _candidates(release: Release, ids: tuple[str, ...]) -> list[Edition]:
        found = [release.editions[i] for i in ids]
        return [e for e in found if e.previewable and e.status == "active"]

    def _resolve_one(self, release: Release, ids: tuple[str, ...], section: int) -> dict:
        if not ids:
            return {"available": False, "reason": "unknown-book"}
        candidates = self._candidates(release, ids)
        if not candidates:
            return {"available": False, "reason": "no-preview"}
        hits = []
        for edition in candidates:
            if edition.corpus_relation not in SEARCHABLE_RELATIONS or edition.page_map is None:
                continue
            pdf_page = edition.page_map.by_legacy.get(section)
            if pdf_page is not None:
                hits.append((edition, pdf_page))
        if len(hits) == 1:
            edition, pdf_page = hits[0]
            return {"available": True, "edition_id": edition.edition_id, "pdf_page": pdf_page,
                    "text_identity": edition.record["corpus"]["text_identity"]}
        if len(hits) > 1:
            return {"available": False, "reason": "ambiguous"}
        return {"available": False, "reason": "page-not-mapped"}

    # -- routes --------------------------------------------------------------
    def _build_router(self) -> APIRouter:
        router = APIRouter()
        lib = self

        @router.get("/api/library/release")
        def library_release(request: Request):
            lib._limit(request, lib.json_limiter)
            release = lib.state.get()
            if release is None:
                return lib._json({"available": False, "schema": SCHEMA, "message": "目录尚未发布或不可用",
                                  "reason": lib.state.error}, None)
            return lib._json({
                "available": True,
                "schema": SCHEMA,
                "generated_at": release.generated_at,
                "counts": release.summary["counts"],
                "coverage": release.summary["coverage"],
                "stages": release.summary["stages"],
                "regions": release.summary["regions"],
                "pending_error": lib.state.error,
            }, release)

        @router.get("/api/library/editions")
        def library_editions(request: Request, phase: Optional[str] = None, subject: Optional[str] = None,
                             q: Optional[str] = None, region: Optional[str] = None,
                             offset: Optional[str] = None, limit: Optional[str] = None):
            lib._limit(request, lib.json_limiter)
            phase = _text_param(phase, MAX_FACET_CHARS, "phase")
            subject = _text_param(subject, MAX_FACET_CHARS, "subject")
            region = _text_param(region, 8, "region")
            terms = _terms(_text_param(q, MAX_Q_CHARS, "q"))
            start = _int_param(offset, "offset", 0, 0, MAX_OFFSET)
            size = _int_param(limit, "limit", DEFAULT_EDITIONS_LIMIT, 1, MAX_LIST_LIMIT)
            release = lib._release()
            pool = [e for e in release.editions.values()
                    if e.previewable and e.status == "active"
                    and (not phase or e.stage == phase) and (not region or e.region == region)]
            subjects = _counts(e.subject for e in pool)
            rows = [e for e in pool if (not subject or e.subject == subject) and _matches(e.haystack, terms)]
            return lib._json({
                "total": len(rows), "offset": start, "limit": size,
                "subjects": subjects,
                "editions": [_edition_card(e) for e in rows[start:start + size]],
            }, release)

        @router.get("/api/library/editions/{edition_id}")
        def library_edition(request: Request, edition_id: str):
            lib._limit(request, lib.json_limiter)
            release = lib._release()
            edition = lib._edition(release, edition_id)
            page_map = edition.page_map
            payload = dict(_edition_card(edition))
            payload.update({
                "source": edition.record["source"],
                "pdf": edition.record["pdf"],
                "corpus": edition.record["corpus"],
                "processing": edition.record["processing"],
                "legacy": edition.record["legacy"],
                "previewable": edition.previewable,
                "searchable": edition.searchable and bool(page_map and page_map.legacy_mapped_pages > 0),
                "file": ({"url": edition.file_url(), "download_url": edition.file_url(download=True),
                          "filename": edition.filename, "bytes": edition.size} if edition.previewable else None),
                "page_map": {
                    "available": page_map is not None,
                    "version": page_map.version if page_map else None,
                    "pages": edition.pages,
                    "printed_verified_pages": page_map.printed_verified_pages if page_map else 0,
                    "legacy_mapped_pages": page_map.legacy_mapped_pages if page_map else 0,
                },
                # Other sources for THIS edition only.
                # (when this file is not the platform's current one, the upstream link would
                # deliver a different file, so it is not offered as a source of this edition)
                "sources": [s for row in release.downloads if row.record["edition_id"] == edition.edition_id
                            for s in row.record["sources"]
                            if s["kind"] != "vps" and not (s["kind"] == "upstream" and edition.record["platform_current"] is False)][:4]
                           or ([edition.record["storage"]["drive"]] if edition.record["storage"]["drive"] else []),
                # A newer / older file of the same book is a different edition with its own entry.
                "newer_edition": _edition_brief(release.editions, release.downloads, edition.record["newer_edition_id"]),
                "supersedes_edition": _edition_brief(release.editions, release.downloads,
                                                     edition.record["supersedes_edition_id"]),
                "metadata_basis": edition.record["metadata_basis"],
                # Different editions of the same work are listed separately, never merged.
                "other_editions": [
                    {"edition_id": other.edition_id, "title": other.title, "edition_label": other.record["edition_label"],
                     "publisher": other.record["publisher"], "status": other.status, "pages": other.pages,
                     "previewable": other.previewable}
                    for other in release.editions.values()
                    if edition.record["work_id"] and other.record["work_id"] == edition.record["work_id"]
                    and other.edition_id != edition.edition_id
                ][:12],
            })
            return lib._json({"edition": payload}, release)

        @router.get("/api/library/editions/{edition_id}/pages")
        def library_pages(request: Request, edition_id: str, to: Optional[str] = None):
            lib._limit(request, lib.json_limiter)
            raw_from = request.query_params.get("from")
            release = lib._release()
            edition = lib._edition(release, edition_id)
            first = _int_param(raw_from, "from", 1, 1, MAX_PAGE_NUMBER)
            last = _int_param(to, "to", first + MAX_PAGE_SPAN - 1, 1, MAX_PAGE_NUMBER + MAX_PAGE_SPAN)
            if last < first:
                raise _bad("to must not be smaller than from")
            if last - first + 1 > MAX_PAGE_SPAN:
                raise _bad(f"page span too large (max {MAX_PAGE_SPAN})")
            if first > edition.pages:
                raise _bad("from beyond last page", 404)
            last = min(last, edition.pages)
            if edition.page_map is None:
                rows = [{"pdf_page": n, "printed_label": None, "printed_verified": False,
                         "legacy_image_index": None, "ocr_page": None} for n in range(first, last + 1)]
            else:
                rows = [edition.page_map.row(n) for n in range(first, last + 1)]
            return lib._json({"edition_id": edition.edition_id, "pages": edition.pages, "from": first, "to": last,
                              "page_map_available": edition.page_map is not None, "rows": rows}, release)

        @router.get("/api/library/resolve")
        def library_resolve(request: Request, book_key: Optional[str] = None, section: Optional[str] = None,
                            items: Optional[str] = None, edition_id: Optional[str] = None,
                            sections: Optional[str] = None):
            lib._limit(request, lib.json_limiter)
            release = lib._release()
            if items is not None:
                # Compact batch: "<short_key>.<section>,<short_key>.<section>" (short_key = 12 hex).
                if len(items) > MAX_ITEMS_PARAM_CHARS:
                    raise _bad("items too long")
                parts = [p for p in items.split(",") if p]
                if not parts or len(parts) > MAX_RESOLVE_ITEMS:
                    raise _bad(f"items must contain 1-{MAX_RESOLVE_ITEMS} entries")
                results = []
                for part in parts:
                    match = re.fullmatch(r"([0-9a-f]{12})\.([0-9]{1,6})", part)
                    if not match:
                        raise _bad("malformed item")
                    outcome = lib._resolve_one(release, release.by_short_key.get(match.group(1), ()), int(match.group(2)))
                    results.append({"item": part, **outcome})
                return lib._json({"results": results}, release)
            if edition_id is not None:
                # In-book search: several legacy indexes of one known edition.
                edition = lib._edition(release, edition_id)
                if sections is None or len(sections) > MAX_ITEMS_PARAM_CHARS:
                    raise _bad("sections required")
                parts = [p for p in sections.split(",") if p]
                if not parts or len(parts) > MAX_RESOLVE_ITEMS or not all(re.fullmatch(r"[0-9]{1,6}", p) for p in parts):
                    raise _bad(f"sections must contain 1-{MAX_RESOLVE_ITEMS} integers")
                usable = (edition.previewable and edition.page_map is not None
                          and edition.corpus_relation in SEARCHABLE_RELATIONS)
                results = []
                for part in parts:
                    pdf_page = edition.page_map.by_legacy.get(int(part)) if usable else None
                    results.append({"section": int(part), "available": pdf_page is not None, "pdf_page": pdf_page,
                                    **({} if pdf_page is not None else {"reason": "page-not-mapped"})})
                return lib._json({"edition_id": edition.edition_id, "results": results}, release)
            if book_key is None or not book_key.strip() or len(book_key) > MAX_BOOK_KEY_CHARS:
                raise _bad("book_key required")
            number = _int_param(section, "section", -1, 0, MAX_PAGE_NUMBER) if section not in (None, "") else -1
            if number < 0:
                raise _bad("section required")
            outcome = lib._resolve_one(release, release.by_sun_key.get(book_key, ()), number)
            return lib._json(outcome, release)

        @router.get("/api/library/legacy")
        def library_legacy(request: Request, kind: Optional[str] = None, ref: Optional[str] = None,
                           page: Optional[str] = None):
            lib._limit(request, lib.json_limiter)
            if kind not in ("jc", "jks"):
                raise _bad("kind must be jc or jks")
            if ref is None or not ref.strip() or len(ref) > MAX_BOOK_KEY_CHARS:
                raise _bad("ref required")
            has_page = page not in (None, "")
            number = None
            page_valid = True
            if has_page:
                if re.fullmatch(r"[0-9]{1,6}", page or ""):
                    number = int(page)
                else:
                    page_valid = False  # old links with a broken page still open the book
            release = lib._release()
            if kind == "jc":
                ids = release.by_sun_key.get(ref, ())
            else:
                if not SLUG_RE.fullmatch(ref):
                    raise _bad("malformed ref")
                ids = release.by_jks_slug.get(ref, ())
            if not ids:
                return lib._json({"available": False, "exact": False, "kind": kind, "reason": "unknown-book"}, release)
            candidates = lib._candidates(release, ids)
            if not candidates:
                return lib._json({"available": False, "exact": False, "kind": kind, "reason": "no-preview"}, release)
            if number is not None:
                hits = []
                for edition in candidates:
                    pdf_page = None
                    if kind == "jc":
                        if edition.page_map is not None and edition.corpus_relation in SEARCHABLE_RELATIONS:
                            pdf_page = edition.page_map.by_legacy.get(number)
                    else:
                        if edition.page_map is not None and edition.page_map.by_jks:
                            pdf_page = edition.page_map.by_jks.get(number)
                        elif edition.jks_pages_are_pdf_pages and 1 <= number <= edition.pages:
                            pdf_page = number
                    if pdf_page is not None:
                        hits.append((edition, pdf_page))
                if len(hits) == 1:
                    edition, pdf_page = hits[0]
                    return lib._json({"available": True, "exact": True, "kind": kind,
                                      "edition_id": edition.edition_id, "pdf_page": pdf_page,
                                      "title": edition.title}, release)
            if len(candidates) != 1:
                return lib._json({"available": False, "exact": False, "kind": kind, "reason": "ambiguous"}, release)
            edition = candidates[0]
            reason = "no-page" if not has_page else ("bad-page" if not page_valid else "page-not-mapped")
            return lib._json({"available": True, "exact": False, "kind": kind, "edition_id": edition.edition_id,
                              "pdf_page": 1, "title": edition.title, "reason": reason}, release)

        def _filter_downloads(release: Release, stage, subject, publisher, region, terms):
            return [row for row in release.downloads
                    if (not stage or row.stage == stage) and (not subject or row.subject == subject)
                    and (not publisher or row.publisher == publisher) and (not region or row.region == region)
                    and _matches(row.haystack, terms)]

        @router.get("/api/library/downloads/facets")
        def library_download_facets(request: Request, stage: Optional[str] = None, subject: Optional[str] = None,
                                    region: Optional[str] = None):
            lib._limit(request, lib.json_limiter)
            stage = _text_param(stage, MAX_FACET_CHARS, "stage")
            subject = _text_param(subject, MAX_FACET_CHARS, "subject")
            region = _text_param(region, 8, "region")
            release = lib._release()
            in_region = [row for row in release.downloads if not region or row.region == region]
            in_stage = [row for row in in_region if not stage or row.stage == stage]
            in_subject = [row for row in in_stage if not subject or row.subject == subject]
            stages = sorted(_counts(row.stage for row in in_region), key=lambda s: (STAGE_ORDER.get(s["name"], 9), s["name"]))
            return lib._json({
                "total": len(in_region),
                "stages": stages,
                "subjects": _counts(row.subject for row in in_stage),
                "publishers": sorted(_counts(row.publisher for row in in_subject), key=lambda p: -p["count"]),
                "regions": release.summary["regions"],
            }, release)

        @router.get("/api/library/downloads")
        def library_downloads(request: Request, stage: Optional[str] = None, subject: Optional[str] = None,
                              publisher: Optional[str] = None, q: Optional[str] = None, region: Optional[str] = None,
                              offset: Optional[str] = None, limit: Optional[str] = None, row: Optional[str] = None):
            lib._limit(request, lib.json_limiter)
            stage = _text_param(stage, MAX_FACET_CHARS, "stage")
            subject = _text_param(subject, MAX_FACET_CHARS, "subject")
            publisher = _text_param(publisher, MAX_FACET_CHARS, "publisher")
            region = _text_param(region, 8, "region")
            terms = _terms(_text_param(q, MAX_Q_CHARS, "q"))
            start = _int_param(offset, "offset", 0, 0, MAX_OFFSET)
            size = _int_param(limit, "limit", DEFAULT_DOWNLOADS_LIMIT, 1, MAX_LIST_LIMIT)
            if row is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}", row):
                raise _bad("malformed row")
            release = lib._release()
            rows = _filter_downloads(release, stage, subject, publisher, region, terms)
            if row is not None:   # one exact entry (used by "官方平台已有更新版本" links)
                rows = [item for item in rows if item.record["row_id"] == row]
            return lib._json({"total": len(rows), "offset": start, "limit": size,
                              "downloads": [row.record for row in rows[start:start + size]]}, release)

        @router.api_route("/files/pdf/{edition_id}/{sha12}/{filename}", methods=["GET", "HEAD"])
        def library_pdf(request: Request, edition_id: str, sha12: str, filename: str, download: Optional[str] = None):
            # `filename` is cosmetic and never used; the real name is derived from the catalog.
            lib._limit(request, lib.pdf_limiter)
            release = lib._release()
            if not SHA12_RE.fullmatch(sha12 or ""):
                raise _bad("not found", 404)
            edition = lib._edition(release, edition_id)
            if not edition.previewable or edition.sha12 != sha12:
                raise _bad("not found", 404)
            disposition = "attachment" if download == "1" else "inline"
            base = {"X-Content-Type-Options": "nosniff", "X-Library-Release": release.release_id}
            # The long-lived cache header goes on successful bodies only (200/206);
            # error and 416 answers must never be stored against this URL.
            good = {
                **base,
                "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(edition.filename, safe='')}",
                "Cache-Control": "public, max-age=31536000, immutable",
            }
            uncached = {**base, "Cache-Control": "no-store"}
            stored_name = f"{edition.sha256}.pdf"          # derived from the catalog only
            path = lib.settings.pdf_root / stored_name
            root_readable = lib.settings.pdf_root.is_dir()

            if lib.settings.serve_mode == "accel":
                if root_readable and not path.is_file():
                    raise HTTPException(status_code=404, detail="file missing", headers={"Cache-Control": "no-store"})
                good["X-Accel-Redirect"] = lib.settings.accel_prefix + stored_name
                # nginx serves the body, Range, Content-Length and its own validators.
                return Response(status_code=200, media_type="application/pdf", headers=good)

            try:
                size = path.stat().st_size if path.is_file() else -1
            except OSError:
                size = -1
            if size < 0:
                raise HTTPException(status_code=404, detail="file missing", headers={"Cache-Control": "no-store"})
            etag = f'"{edition.sha256}"'
            if _etag_matches(request.headers.get("if-none-match"), etag):
                return Response(status_code=304, headers={**base, "ETag": etag})
            byte_range = _parse_range(request.headers.get("range"), size)
            if_range = request.headers.get("if-range")
            if if_range is not None and if_range != etag:
                byte_range = None
            if byte_range == "invalid":
                return Response(status_code=416, headers={**uncached, "Accept-Ranges": "bytes",
                                                          "Content-Range": f"bytes */{size}"})
            good.update({"Accept-Ranges": "bytes", "ETag": etag})
            if byte_range is None:
                start, length, status = 0, size, 200
            else:
                start, end = byte_range
                length, status = end - start + 1, 206
                good["Content-Range"] = f"bytes {start}-{end}/{size}"
            good["Content-Length"] = str(length)
            if request.method == "HEAD":
                return Response(status_code=status, media_type="application/pdf", headers=good)
            return StreamingResponse(_file_stream(path, start, length), status_code=status,
                                     media_type="application/pdf", headers=good)

        return router


def install(app: FastAPI, data_root: Path, frontend_dir: Optional[Path] = None,
            env: Optional[Mapping[str, str]] = None, clock: Callable[[], float] = time.monotonic) -> Library:
    """Attach the library router to ``app``.  Never raises because of catalog
    problems: a missing or invalid release only makes the two tabs report
    "目录尚未发布/不可用"."""
    library = Library(Settings.from_env(Path(data_root), env), clock)
    app.include_router(library.router)
    vendor = Path(frontend_dir) / "vendor" if frontend_dir else None
    if vendor is not None and vendor.is_dir():
        app.mount("/vendor", StaticFiles(directory=vendor), name="library-vendor")

    def _library_startup_load() -> None:
        release = library.state.get()
        if release is None:
            print(f"[library] catalog unavailable: {library.state.error}", flush=True)
        else:
            print(f"[library] catalog release {release.release_id}: "
                  f"{release.summary['counts']['previewable']} previewable, "
                  f"{release.summary['counts']['downloads']} downloads", flush=True)

    # Same registration style as main.py.  The release is also loaded lazily on
    # first use, so this hook is only a warm-up and a log line.
    app.on_event("startup")(_library_startup_load)
    app.state.library = library
    return library
