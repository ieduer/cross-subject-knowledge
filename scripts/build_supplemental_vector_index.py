#!/usr/bin/env python3
"""
Build or verify the supplemental textbook page vector index.

Examples:
  python3 scripts/build_supplemental_vector_index.py build
  python3 scripts/build_supplemental_vector_index.py verify
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import faiss
    import numpy as np
    from sentence_transformers import SentenceTransformer
except ImportError:
    faiss = None
    np = None
    SentenceTransformer = None


ROOT = Path(__file__).resolve().parents[1]


def _default_data_root() -> Path:
    primary = ROOT / "data"
    alt = ROOT.parent / "data"
    if not (primary / "index").exists() and (alt / "index").exists():
        return alt
    return primary


DATA_ROOT = Path(os.getenv("DATA_ROOT", _default_data_root())).expanduser().resolve()
DEFAULT_SOURCE = ROOT / "backend" / "supplemental_textbook_pages.jsonl.gz"
if not DEFAULT_SOURCE.exists():
    DEFAULT_SOURCE = DATA_ROOT / "index" / "supplemental_textbook_pages.jsonl.gz"
DEFAULT_INDEX = DATA_ROOT / "index" / "supplemental_textbook_pages.index"
DEFAULT_MANIFEST = DATA_ROOT / "index" / "supplemental_textbook_pages.vector.manifest.json"

DEFAULT_MODEL = os.getenv("EMBEDDER", "BAAI/bge-m3")
DEFAULT_TEXT_LIMIT = int(os.getenv("SUPPLEMENTAL_VECTOR_TEXT_LIMIT_CHARS", "512"))
DEFAULT_BATCH_SIZE = int(os.getenv("SUPPLEMENTAL_VECTOR_BATCH_SIZE", "64"))
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def write_json_atomic(path: Path, payload: dict) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def write_index_atomic(path: Path, index) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    faiss.write_index(index, str(tmp_path))
    tmp_path.replace(path)


def load_pages(source_path: Path, text_limit: int) -> tuple[list[str], list[str]]:
    opener = gzip.open if source_path.suffix == ".gz" else open
    entry_ids: list[str] = []
    texts: list[str] = []
    with opener(source_path, "rt", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            entry_id = str(item.get("id") or "").strip()
            text = str(item.get("text") or "").strip()
            if not entry_id or not text:
                continue
            entry_ids.append(entry_id)
            texts.append(text[:text_limit])
    return entry_ids, texts


def has_local_sentence_transformer_snapshot(model_name: str) -> bool:
    direct_path = Path(model_name).expanduser()
    if direct_path.exists():
        return True
    snapshots_dir = (
        Path.home()
        / ".cache"
        / "huggingface"
        / "hub"
        / f"models--{model_name.replace('/', '--')}"
        / "snapshots"
    )
    if not snapshots_dir.exists():
        return False
    return any(path.is_dir() for path in snapshots_dir.iterdir())


def compute_fingerprint(entry_ids: list[str], texts: list[str]) -> str:
    h = hashlib.sha256()
    for entry_id, text in zip(entry_ids, texts):
        payload = json.dumps([entry_id, text], ensure_ascii=False, separators=(",", ":"))
        h.update(payload.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def build_manifest(
    *,
    source_path: Path,
    index_path: Path,
    model_name: str,
    dimension: int,
    vector_rows: int,
    text_limit: int,
    batch_size: int,
    fingerprint: str,
) -> dict:
    return {
        "schema_version": 1,
        "built_at": utc_now_iso(),
        "index": {
            "path": index_path.name,
            "type": "IndexFlatIP",
            "metric": "inner_product_on_normalized_embeddings",
            "dimension": dimension,
            "vector_rows": vector_rows,
        },
        "model": {
            "name": model_name,
            "text_limit_chars": text_limit,
            "batch_size": batch_size,
        },
        "vector_source": {
            "path": str(source_path),
            "row_count": vector_rows,
            "fingerprint_sha256": fingerprint,
            "sha256": sha256_file(source_path),
            "size_bytes": source_path.stat().st_size,
        },
    }


def run_build(args: argparse.Namespace) -> int:
    if faiss is None or np is None or SentenceTransformer is None:
        print("ERROR: faiss-cpu, numpy, and sentence-transformers are required", file=sys.stderr)
        return 2

    source_path = args.source.expanduser().resolve()
    index_path = args.index.expanduser().resolve()
    manifest_path = args.manifest.expanduser().resolve()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    if not source_path.exists():
        print(f"ERROR: source file missing: {source_path}", file=sys.stderr)
        return 1

    print(f"Loading source pages from {source_path}...", file=sys.stderr)
    entry_ids, texts = load_pages(source_path, args.text_limit)
    if not entry_ids:
        print("ERROR: no supplemental pages found", file=sys.stderr)
        return 1

    print(f"Loading SentenceTransformer model {args.model}...", file=sys.stderr)
    model_kwargs = {}
    if has_local_sentence_transformer_snapshot(args.model):
        model_kwargs["local_files_only"] = True
        os.environ["HF_HUB_OFFLINE"] = "1"
    model = SentenceTransformer(args.model, **model_kwargs)
    fingerprint = compute_fingerprint(entry_ids, texts)

    index = None
    start_time = time.time()
    total = len(entry_ids)
    for start in range(0, total, args.batch_size):
        batch_texts = texts[start:start + args.batch_size]
        embeddings = model.encode(batch_texts, show_progress_bar=False, normalize_embeddings=True)
        matrix = np.asarray(embeddings, dtype="float32")
        if index is None:
            index = faiss.IndexFlatIP(int(matrix.shape[1]))
        index.add(matrix)
        if start and start % max(args.batch_size * 10, 512) == 0:
            elapsed = max(time.time() - start_time, 0.001)
            rate = start / elapsed
            print(f"Processed {start}/{total} pages ({(start / total) * 100:.1f}%) at {rate:.1f} pages/s", file=sys.stderr)

    elapsed = time.time() - start_time
    manifest = build_manifest(
        source_path=source_path,
        index_path=index_path,
        model_name=args.model,
        dimension=index.d,
        vector_rows=index.ntotal,
        text_limit=args.text_limit,
        batch_size=args.batch_size,
        fingerprint=fingerprint,
    )

    write_index_atomic(index_path, index)
    write_json_atomic(manifest_path, manifest)
    print(
        json.dumps(
            {
                "ok": True,
                "source": str(source_path),
                "index": str(index_path),
                "manifest": str(manifest_path),
                "vector_rows": index.ntotal,
                "dimension": index.d,
                "elapsed_sec": round(elapsed, 2),
            },
            ensure_ascii=False,
        )
    )
    return 0


def run_verify(args: argparse.Namespace) -> int:
    if faiss is None:
        print("ERROR: faiss-cpu is required", file=sys.stderr)
        return 2

    source_path = args.source.expanduser().resolve()
    index_path = args.index.expanduser().resolve()
    manifest_path = args.manifest.expanduser().resolve()

    result: dict[str, object] = {
        "ok": False,
        "source": str(source_path),
        "index": str(index_path),
        "manifest": str(manifest_path),
        "checks": [],
    }
    checks: list[dict[str, object]] = result["checks"]  # type: ignore[assignment]

    for path, name in ((source_path, "source_exists"), (index_path, "index_exists"), (manifest_path, "manifest_exists")):
        if not path.exists():
            checks.append({"name": name, "ok": False, "detail": "missing"})
            print(json.dumps(result, ensure_ascii=False))
            return 1
        checks.append({"name": name, "ok": True})

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    text_limit = int((manifest.get("model") or {}).get("text_limit_chars") or args.text_limit)
    entry_ids, texts = load_pages(source_path, text_limit)
    fingerprint = compute_fingerprint(entry_ids, texts)

    index = faiss.read_index(str(index_path))
    expected_rows = len(entry_ids)

    checks.append({"name": "vector_rows_match_source", "ok": index.ntotal == expected_rows, "detail": f"index={index.ntotal}, source={expected_rows}"})
    checks.append({"name": "manifest_rows_match_index", "ok": (manifest.get("index") or {}).get("vector_rows") == index.ntotal})
    checks.append({"name": "manifest_model", "ok": (manifest.get("model") or {}).get("name") == args.model, "detail": (manifest.get("model") or {}).get("name")})
    checks.append({"name": "fingerprint_match", "ok": (manifest.get("vector_source") or {}).get("fingerprint_sha256") == fingerprint})
    checks.append({"name": "source_sha256_match", "ok": (manifest.get("vector_source") or {}).get("sha256") == sha256_file(source_path)})

    ok = all(bool(item.get("ok")) for item in checks)
    result["ok"] = ok
    result["vector_rows"] = index.ntotal
    result["dimension"] = index.d
    print(json.dumps(result, ensure_ascii=False))
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build supplemental textbook page vector index.")
    sub = parser.add_subparsers(dest="command", required=False)

    for name in ("build", "verify"):
        subparser = sub.add_parser(name)
        subparser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
        subparser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
        subparser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
        subparser.add_argument("--model", default=DEFAULT_MODEL)
        subparser.add_argument("--text-limit", type=int, default=DEFAULT_TEXT_LIMIT)
        if name == "build":
            subparser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
        else:
            subparser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    command = args.command or "build"
    if command == "build":
        return run_build(args)
    if command == "verify":
        return run_verify(args)
    parser.error(f"unknown command: {command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
