"""Shared helpers for the sun-catalog-v1 tooling (stdlib only)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Iterable

CONTRACT = "sun-catalog-v1"
TOOL_VERSION = "2026.10.11-4"
EXTRACTOR_VERSION = "pymupdf-text-v2"
PAGE_MAP_VERSION = "page-map-v4"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MD5_RE = re.compile(r"^[0-9a-f]{32}$")


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def dumps(obj: Any) -> str:
    """Deterministic JSON text: sorted keys, fixed separators, trailing newline."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1) + "\n"


def write_atomic(path: os.PathLike | str, data: bytes | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: os.PathLike | str, obj: Any) -> None:
    write_atomic(path, dumps(obj))


def read_json(path: os.PathLike | str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def file_hashes(path: os.PathLike | str, algos: Iterable[str] = ("sha256", "md5")) -> dict[str, Any]:
    hs = {a: hashlib.new(a) for a in algos}
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            for h in hs.values():
                h.update(chunk)
    out: dict[str, Any] = {a: h.hexdigest() for a, h in hs.items()}
    out["bytes"] = size
    return out


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fold(text: str) -> str:
    """Comparison form: NFKC, lower-case, drop whitespace / punctuation / symbols / controls."""
    out = []
    for ch in unicodedata.normalize("NFKC", text or ""):
        cat = unicodedata.category(ch)
        if cat[0] in "PSZC":
            continue
        out.append(ch.lower())
    return "".join(out)


def jc_short_key(book_key: str) -> str:
    """JC / page-image directory key: md5(book_key)[:12] (verified 144/144)."""
    return hashlib.md5(book_key.encode("utf-8")).hexdigest()[:12]
