#!/usr/bin/env python3
"""Build, refresh, or verify the primary textbook FAISS index.

The primary dense index is derived from the immutable textbook subset of
``textbook_mineru_fts.db``. A successful build atomically replaces each output
file and publishes the fingerprint manifest last; ``verify`` fails closed when
the database, index, IDs, fingerprint, model, or vector dimensions no longer
match.

Examples:
  python3 scripts/build_primary_vector_index.py build
  python3 scripts/build_primary_vector_index.py verify
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


faiss = None
np = None
SentenceTransformer = None

ROOT = Path(__file__).resolve().parents[1]


def _default_data_root() -> Path:
    primary = ROOT / "data"
    alternate = ROOT.parent / "data"
    if not (primary / "index").exists() and (alternate / "index").exists():
        return alternate
    return primary


DATA_ROOT = Path(os.getenv("DATA_ROOT", _default_data_root())).expanduser().resolve()
DEFAULT_DB = DATA_ROOT / "index" / "textbook_mineru_fts.db"
DEFAULT_INDEX = DATA_ROOT / "index" / "textbook_chunks.index"
DEFAULT_MANIFEST = DATA_ROOT / "index" / "textbook_chunks.manifest.json"
DEFAULT_MODEL = os.getenv("EMBEDDER", "BAAI/bge-m3")
DEFAULT_TEXT_LIMIT = int(os.getenv("VECTOR_TEXT_LIMIT_CHARS", "512"))
DEFAULT_BATCH_SIZE = int(os.getenv("VECTOR_BATCH_SIZE", "128"))
SOURCE_SQL = "source != 'gaokao' AND text IS NOT NULL AND text != ''"
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_faiss() -> None:
    global faiss
    if faiss is None:
        import faiss as faiss_module

        faiss = faiss_module


def ensure_numpy() -> None:
    global np
    if np is None:
        import numpy as np_module

        np = np_module


def ensure_sentence_transformer() -> None:
    global SentenceTransformer
    if SentenceTransformer is None:
        from sentence_transformers import SentenceTransformer as sentence_transformer_cls

        SentenceTransformer = sentence_transformer_cls


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def connect_vector_db(path: Path) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_chunks(db_path: Path, text_limit: int) -> tuple[list[int], list[str]]:
    with connect_vector_db(db_path) as conn:
        rows = conn.execute(
            f"SELECT id, text FROM chunks WHERE {SOURCE_SQL} ORDER BY id"
        ).fetchall()
    chunk_ids = [int(row["id"]) for row in rows]
    texts = [str(row["text"] or "")[:text_limit] for row in rows]
    return chunk_ids, texts


def compute_vector_source_fingerprint(chunk_ids: list[int], texts: list[str]) -> str:
    digest = hashlib.sha256()
    for chunk_id, text in zip(chunk_ids, texts):
        payload = json.dumps([chunk_id, text], ensure_ascii=False, separators=(",", ":"))
        digest.update(payload.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def write_index_atomic(path: Path, index: object) -> None:
    ensure_faiss()
    temporary = path.with_suffix(path.suffix + ".tmp")
    faiss.write_index(index, str(temporary))
    temporary.replace(path)


def _model_cache_roots() -> list[Path]:
    roots: list[Path] = []
    if os.getenv("HF_HUB_CACHE"):
        roots.append(Path(os.environ["HF_HUB_CACHE"]).expanduser())
    if os.getenv("HF_HOME"):
        roots.append(Path(os.environ["HF_HOME"]).expanduser() / "hub")
    roots.append(Path.home() / ".cache" / "huggingface" / "hub")
    return roots


def has_local_model_snapshot(model_name: str) -> bool:
    direct_path = Path(model_name).expanduser()
    if direct_path.exists():
        return True
    cache_key = f"models--{model_name.replace('/', '--')}"
    for root in _model_cache_roots():
        snapshots = root / cache_key / "snapshots"
        if snapshots.exists() and any(path.is_dir() for path in snapshots.iterdir()):
            return True
    return False


def model_kwargs(model_name: str) -> dict[str, object]:
    if has_local_model_snapshot(model_name):
        os.environ["HF_HUB_OFFLINE"] = "1"
        return {"local_files_only": True}
    return {}


def build_manifest(
    *,
    db_path: Path,
    index_path: Path,
    model_name: str,
    dimension: int,
    vector_rows: int,
    text_limit: int,
    batch_size: int,
    vector_source_fingerprint: str,
    index_sha256: str,
    index_size_bytes: int,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "built_at": utc_now_iso(),
        "index": {
            "path": index_path.name,
            "type": "IndexIDMap(IndexFlatIP)",
            "metric": "inner_product_on_normalized_embeddings",
            "dimension": dimension,
            "vector_rows": vector_rows,
            "sha256": index_sha256,
            "size_bytes": index_size_bytes,
        },
        "model": {
            "name": model_name,
            "text_limit_chars": text_limit,
            "batch_size": batch_size,
        },
        "vector_source": {
            "source_sql": SOURCE_SQL,
            "row_count": vector_rows,
            "fingerprint_sha256": vector_source_fingerprint,
        },
        "db": {
            "path": db_path.name,
            "sha256": sha256_file(db_path),
            "size_bytes": db_path.stat().st_size,
            "source_sql": SOURCE_SQL,
            "row_count": vector_rows,
        },
    }


def index_ids(index: object) -> list[int] | None:
    if not hasattr(index, "id_map"):
        return None
    ensure_faiss()
    return [int(item) for item in faiss.vector_to_array(index.id_map)]


def index_inner_type(index: object) -> str | None:
    if not hasattr(index, "index"):
        return None
    ensure_faiss()
    return type(faiss.downcast_index(index.index)).__name__


def resolve_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    return (
        args.db.expanduser().resolve(),
        args.index.expanduser().resolve(),
        args.manifest.expanduser().resolve(),
    )


def run_build(args: argparse.Namespace) -> int:
    try:
        ensure_faiss()
        ensure_numpy()
        ensure_sentence_transformer()
    except ImportError:
        print("ERROR: faiss-cpu, numpy, and sentence-transformers are required", file=sys.stderr)
        return 2

    db_path, index_path, manifest_path = resolve_paths(args)
    if not db_path.is_file():
        print(f"ERROR: database missing: {db_path}", file=sys.stderr)
        return 1
    index_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    chunk_ids, texts = load_chunks(db_path, args.text_limit)
    if not chunk_ids:
        print("ERROR: no primary textbook chunks found", file=sys.stderr)
        return 1
    if len(chunk_ids) != len(set(chunk_ids)):
        print("ERROR: duplicate chunk IDs in primary vector source", file=sys.stderr)
        return 1

    print(f"Loading SentenceTransformer model {args.model}...", file=sys.stderr)
    model = SentenceTransformer(args.model, **model_kwargs(args.model))
    total = len(chunk_ids)
    print(f"Embedding {total} primary textbook chunks...", file=sys.stderr)
    started = time.time()
    index = None

    for start in range(0, total, args.batch_size):
        batch_texts = texts[start:start + args.batch_size]
        batch_ids = chunk_ids[start:start + args.batch_size]
        embeddings = model.encode(
            batch_texts,
            show_progress_bar=False,
            normalize_embeddings=True,
        )
        matrix = np.asarray(embeddings, dtype="float32")
        if index is None:
            index = faiss.IndexIDMap(faiss.IndexFlatIP(int(matrix.shape[1])))
        index.add_with_ids(matrix, np.asarray(batch_ids, dtype="int64"))

        processed = min(start + len(batch_texts), total)
        if processed % max(args.batch_size * 10, 512) == 0 or processed == total:
            elapsed = max(time.time() - started, 0.001)
            print(
                f"Processed {processed}/{total} chunks "
                f"({(processed / total) * 100:.1f}%) at {processed / elapsed:.1f} chunks/s",
                file=sys.stderr,
            )

    elapsed = time.time() - started
    fingerprint = compute_vector_source_fingerprint(chunk_ids, texts)
    write_index_atomic(index_path, index)
    index_sha256 = sha256_file(index_path)
    manifest = build_manifest(
        db_path=db_path,
        index_path=index_path,
        model_name=args.model,
        dimension=int(index.d),
        vector_rows=int(index.ntotal),
        text_limit=args.text_limit,
        batch_size=args.batch_size,
        vector_source_fingerprint=fingerprint,
        index_sha256=index_sha256,
        index_size_bytes=index_path.stat().st_size,
    )
    write_json_atomic(manifest_path, manifest)
    print(
        json.dumps(
            {
                "ok": True,
                "index": str(index_path),
                "manifest": str(manifest_path),
                "model": args.model,
                "dimension": int(index.d),
                "vector_rows": int(index.ntotal),
                "vector_source_fingerprint": fingerprint,
                "index_sha256": index_sha256,
                "elapsed_sec": round(elapsed, 2),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _append_check(
    checks: list[dict[str, object]],
    name: str,
    ok: bool,
    detail: object | None = None,
) -> None:
    item: dict[str, object] = {"name": name, "ok": bool(ok)}
    if detail is not None:
        item["detail"] = detail
    checks.append(item)


def run_verify(args: argparse.Namespace) -> int:
    try:
        ensure_faiss()
    except ImportError:
        print("ERROR: faiss-cpu is required", file=sys.stderr)
        return 2

    db_path, index_path, manifest_path = resolve_paths(args)
    result: dict[str, object] = {
        "ok": False,
        "db": str(db_path),
        "index": str(index_path),
        "manifest": str(manifest_path),
        "checks": [],
    }
    checks: list[dict[str, object]] = result["checks"]  # type: ignore[assignment]
    for path, name in (
        (db_path, "db_exists"),
        (index_path, "index_exists"),
        (manifest_path, "manifest_exists"),
    ):
        _append_check(checks, name, path.is_file(), None if path.is_file() else "missing")
    if not all(item["ok"] for item in checks):
        print(json.dumps(result, ensure_ascii=False))
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    model_payload = manifest.get("model") or {}
    index_payload = manifest.get("index") or {}
    source_payload = manifest.get("vector_source") or {}
    db_payload = manifest.get("db") or {}
    text_limit = int(model_payload.get("text_limit_chars") or args.text_limit)
    chunk_ids, texts = load_chunks(db_path, text_limit)
    expected_fingerprint = compute_vector_source_fingerprint(chunk_ids, texts)
    current_db_sha256 = sha256_file(db_path)
    current_db_size = db_path.stat().st_size
    current_index_sha256 = sha256_file(index_path)
    current_index_size = index_path.stat().st_size
    index = faiss.read_index(str(index_path))
    stored_ids = index_ids(index)
    inner_type = index_inner_type(index)

    _append_check(checks, "manifest_schema", manifest.get("schema_version") == 2, manifest.get("schema_version"))
    _append_check(checks, "manifest_model", model_payload.get("name") == args.model, model_payload.get("name"))
    _append_check(
        checks,
        "manifest_text_limit",
        int(model_payload.get("text_limit_chars", -1)) == args.text_limit,
        model_payload.get("text_limit_chars"),
    )
    _append_check(
        checks,
        "manifest_index_type",
        index_payload.get("type") == "IndexIDMap(IndexFlatIP)",
        index_payload.get("type"),
    )
    _append_check(
        checks,
        "manifest_index_metric",
        index_payload.get("metric") == "inner_product_on_normalized_embeddings",
        index_payload.get("metric"),
    )
    _append_check(
        checks,
        "manifest_source_sql",
        source_payload.get("source_sql") == SOURCE_SQL,
        source_payload.get("source_sql"),
    )
    _append_check(
        checks,
        "manifest_db_source_sql",
        db_payload.get("source_sql") == SOURCE_SQL,
        db_payload.get("source_sql"),
    )
    _append_check(
        checks,
        "manifest_db_sha256",
        db_payload.get("sha256") == current_db_sha256,
        db_payload.get("sha256"),
    )
    _append_check(
        checks,
        "manifest_db_size",
        int(db_payload.get("size_bytes", -1)) == current_db_size,
        db_payload.get("size_bytes"),
    )
    _append_check(checks, "db_rows", bool(chunk_ids), len(chunk_ids))
    _append_check(
        checks,
        "manifest_db_rows",
        int(db_payload.get("row_count", -1)) == len(chunk_ids),
        db_payload.get("row_count"),
    )
    _append_check(
        checks,
        "manifest_index_sha256",
        index_payload.get("sha256") == current_index_sha256,
        index_payload.get("sha256"),
    )
    _append_check(
        checks,
        "manifest_index_size",
        int(index_payload.get("size_bytes", -1)) == current_index_size,
        index_payload.get("size_bytes"),
    )
    _append_check(checks, "index_outer_type", type(index).__name__ == "IndexIDMap", type(index).__name__)
    _append_check(checks, "index_inner_type", inner_type == "IndexFlatIP", inner_type)
    _append_check(
        checks,
        "index_metric",
        getattr(index, "metric_type", None) == faiss.METRIC_INNER_PRODUCT,
        getattr(index, "metric_type", None),
    )
    _append_check(checks, "index_rows", int(index.ntotal) == len(chunk_ids), int(index.ntotal))
    _append_check(
        checks,
        "manifest_rows",
        int(index_payload.get("vector_rows", -1)) == len(chunk_ids),
        index_payload.get("vector_rows"),
    )
    _append_check(
        checks,
        "manifest_source_rows",
        int(source_payload.get("row_count", -1)) == len(chunk_ids),
        source_payload.get("row_count"),
    )
    _append_check(
        checks,
        "manifest_dimension",
        int(index_payload.get("dimension", -1)) == int(index.d),
        index_payload.get("dimension"),
    )
    _append_check(checks, "index_id_map", stored_ids is not None, "present" if stored_ids is not None else "missing")
    if stored_ids is not None:
        _append_check(checks, "index_ids", stored_ids == chunk_ids, len(stored_ids))
    _append_check(
        checks,
        "vector_source_fingerprint",
        source_payload.get("fingerprint_sha256") == expected_fingerprint,
        source_payload.get("fingerprint_sha256"),
    )

    result.update(
        {
            "ok": all(bool(item["ok"]) for item in checks),
            "db_file_sha256": current_db_sha256,
            "index_file_sha256": current_index_sha256,
            "vector_source_fingerprint": expected_fingerprint,
            "index_type": type(index).__name__,
            "dimension": int(index.d),
            "vector_rows": int(index.ntotal),
        }
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


def run_manifest(args: argparse.Namespace) -> int:
    try:
        ensure_faiss()
    except ImportError:
        print("ERROR: faiss-cpu is required", file=sys.stderr)
        return 2

    db_path, index_path, manifest_path = resolve_paths(args)
    if not db_path.is_file() or not index_path.is_file() or not manifest_path.is_file():
        print(
            "ERROR: database, index, and an existing independently verified manifest "
            "are required for manifest re-emission",
            file=sys.stderr,
        )
        return 1
    chunk_ids, texts = load_chunks(db_path, args.text_limit)
    index = faiss.read_index(str(index_path))
    stored_ids = index_ids(index)
    if int(index.ntotal) != len(chunk_ids) or stored_ids != chunk_ids:
        print("ERROR: index rows or IDs do not match the current vector source", file=sys.stderr)
        return 1
    existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    existing_index = existing_manifest.get("index") or {}
    existing_model = existing_manifest.get("model") or {}
    existing_source = existing_manifest.get("vector_source") or {}
    existing_db = existing_manifest.get("db") or {}
    current_fingerprint = compute_vector_source_fingerprint(chunk_ids, texts)
    current_db_sha256 = sha256_file(db_path)
    current_db_size = db_path.stat().st_size
    current_index_sha256 = sha256_file(index_path)
    current_index_size = index_path.stat().st_size
    inner_type = index_inner_type(index)
    exact_index_metadata = (
        existing_index.get("sha256") == current_index_sha256
        and int(existing_index.get("size_bytes", -1)) == current_index_size
    )
    legacy_index_metadata_missing = (
        "sha256" not in existing_index
        and "size_bytes" not in existing_index
    )
    exact_legacy_db_identity = (
        existing_db.get("sha256") == current_db_sha256
        and int(existing_db.get("size_bytes", -1)) == current_db_size
        and existing_db.get("source_sql") == SOURCE_SQL
        and int(existing_db.get("row_count", -1)) == len(chunk_ids)
    )
    index_metadata_ok = exact_index_metadata or (
        args.allow_legacy_index_metadata_upgrade
        and legacy_index_metadata_missing
        and exact_legacy_db_identity
    )
    existing_identity_ok = (
        existing_manifest.get("schema_version") == 2
        and existing_model.get("name") == args.model
        and int(existing_model.get("text_limit_chars", -1)) == args.text_limit
        and existing_index.get("type") == "IndexIDMap(IndexFlatIP)"
        and existing_index.get("metric") == "inner_product_on_normalized_embeddings"
        and index_metadata_ok
        and existing_source.get("source_sql") == SOURCE_SQL
        and int(existing_source.get("row_count", -1)) == len(chunk_ids)
        and existing_source.get("fingerprint_sha256") == current_fingerprint
        and int(existing_index.get("vector_rows", -1)) == int(index.ntotal)
        and int(existing_index.get("dimension", -1)) == int(index.d)
        and type(index).__name__ == "IndexIDMap"
        and inner_type == "IndexFlatIP"
        and getattr(index, "metric_type", None) == faiss.METRIC_INNER_PRODUCT
    )
    if not existing_identity_ok:
        print(
            "ERROR: existing manifest no longer proves this DB/index vector identity; "
            "run a full build instead of re-emitting metadata",
            file=sys.stderr,
        )
        return 1

    manifest = build_manifest(
        db_path=db_path,
        index_path=index_path,
        model_name=args.model,
        dimension=int(index.d),
        vector_rows=int(index.ntotal),
        text_limit=args.text_limit,
        batch_size=args.batch_size,
        vector_source_fingerprint=current_fingerprint,
        index_sha256=current_index_sha256,
        index_size_bytes=current_index_size,
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(manifest_path, manifest)
    print(json.dumps({"ok": True, "manifest": str(manifest_path)}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build or verify the primary textbook FAISS index.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("build", "verify", "manifest"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--db", type=Path, default=DEFAULT_DB)
        subparser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
        subparser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
        subparser.add_argument("--model", default=DEFAULT_MODEL)
        subparser.add_argument("--text-limit", type=positive_int, default=DEFAULT_TEXT_LIMIT)
        if command in {"build", "manifest"}:
            subparser.add_argument("--batch-size", type=positive_int, default=DEFAULT_BATCH_SIZE)
        if command == "manifest":
            subparser.add_argument(
                "--allow-legacy-index-metadata-upgrade",
                action="store_true",
                help=(
                    "Allow a one-time upgrade only when index hash/size are absent "
                    "and the legacy manifest exactly matches the immutable DB identity."
                ),
            )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    command = args.command
    if command == "build":
        return run_build(args)
    if command == "verify":
        return run_verify(args)
    if command == "manifest":
        return run_manifest(args)
    parser.error(f"unknown command: {command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
