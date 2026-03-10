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
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

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
    ensure_faiss()
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
    source_sha256: str,
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
            "sha256": source_sha256,
            "size_bytes": source_path.stat().st_size,
        },
    }


def _model_kwargs(model_name: str) -> dict[str, object]:
    kwargs: dict[str, object] = {}
    if has_local_sentence_transformer_snapshot(model_name):
        kwargs["local_files_only"] = True
        os.environ["HF_HUB_OFFLINE"] = "1"
    return kwargs


def encode_batch_direct(model_name: str, batch_texts: list[str], batch_size: int) -> "np.ndarray":
    ensure_numpy()
    ensure_sentence_transformer()
    model = SentenceTransformer(model_name, **_model_kwargs(model_name))
    embeddings = model.encode(
        batch_texts,
        batch_size=min(batch_size, len(batch_texts)),
        show_progress_bar=False,
        normalize_embeddings=True,
    )
    return np.asarray(embeddings, dtype="float32")


def encode_batch_isolated(model_name: str, batch_texts: list[str], batch_size: int) -> "np.ndarray":
    ensure_numpy()
    with tempfile.TemporaryDirectory(prefix="supp-vec-") as tmpdir:
        tmp_path = Path(tmpdir)
        input_path = tmp_path / "batch.json"
        output_path = tmp_path / "batch.npy"
        input_path.write_text(json.dumps({"texts": batch_texts}, ensure_ascii=False), encoding="utf-8")

        env = os.environ.copy()
        env.setdefault("TOKENIZERS_PARALLELISM", "false")
        if has_local_sentence_transformer_snapshot(model_name):
            env["HF_HUB_OFFLINE"] = "1"

        worker_code = """
import json
import os
import sys
import numpy as np
from sentence_transformers import SentenceTransformer

input_path, output_path, model_name, batch_size = sys.argv[1:5]
payload = json.loads(open(input_path, 'r', encoding='utf-8').read())
texts = [str(item).strip() for item in payload.get('texts') or [] if str(item).strip()]
kwargs = {}
if os.getenv('HF_HUB_OFFLINE') == '1':
    kwargs['local_files_only'] = True
model = SentenceTransformer(model_name, **kwargs)
embeddings = model.encode(
    texts,
    batch_size=min(int(batch_size), len(texts)),
    show_progress_bar=False,
    normalize_embeddings=True,
)
np.save(output_path, np.asarray(embeddings, dtype='float32'))
"""
        result = subprocess.run(
            [
                sys.executable,
                "-u",
                "-X",
                "faulthandler",
                "-c",
                worker_code,
                str(input_path),
                str(output_path),
                model_name,
                str(batch_size),
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"isolated batch encode failed: {detail[:1000]}")
        return np.load(output_path)


def run_build(args: argparse.Namespace) -> int:
    try:
        ensure_faiss()
        ensure_numpy()
        if args.worker_mode == "direct":
            ensure_sentence_transformer()
    except ImportError:
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

    source_sha256 = sha256_file(source_path)
    print(f"Loading source pages from {source_path}...", file=sys.stderr)
    entry_ids, texts = load_pages(source_path, args.text_limit)
    if not entry_ids:
        print("ERROR: no supplemental pages found", file=sys.stderr)
        return 1

    fingerprint = compute_fingerprint(entry_ids, texts)
    worker_mode = args.worker_mode
    if worker_mode == "auto":
        worker_mode = "isolated" if sys.platform == "darwin" else "direct"

    print(f"Embedding mode: {worker_mode}", file=sys.stderr)
    model = None
    if worker_mode == "direct":
        print(f"Loading SentenceTransformer model {args.model}...", file=sys.stderr)
        model = SentenceTransformer(args.model, **_model_kwargs(args.model))

    start_time = time.time()
    total = len(entry_ids)
    index = None
    for start in range(0, total, args.batch_size):
        batch_texts = texts[start:start + args.batch_size]
        if worker_mode == "direct":
            embeddings = model.encode(
                batch_texts,
                batch_size=min(args.batch_size, len(batch_texts)),
                show_progress_bar=False,
                normalize_embeddings=True,
            )
            matrix = np.asarray(embeddings, dtype="float32")
        else:
            matrix = encode_batch_isolated(args.model, batch_texts, args.batch_size)

        if index is None:
            index = faiss.IndexFlatIP(int(matrix.shape[1]))
        index.add(matrix)

        processed = min(start + len(batch_texts), total)
        if processed % max(args.batch_size * 10, 512) == 0 or processed == total:
            elapsed = max(time.time() - start_time, 0.001)
            rate = processed / elapsed
            print(
                f"Processed {processed}/{total} pages ({(processed / total) * 100:.1f}%) at {rate:.1f} pages/s",
                file=sys.stderr,
            )

    elapsed = time.time() - start_time
    manifest = build_manifest(
        source_path=source_path,
        index_path=index_path,
        source_sha256=source_sha256,
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


def run_encode_batch(args: argparse.Namespace) -> int:
    try:
        ensure_numpy()
        ensure_sentence_transformer()
    except ImportError:
        print("ERROR: numpy and sentence-transformers are required", file=sys.stderr)
        return 2

    input_path = args.input.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    texts = [str(item).strip() for item in payload.get("texts") or [] if str(item).strip()]
    if not texts:
        print("ERROR: no batch texts provided", file=sys.stderr)
        return 1

    matrix = encode_batch_direct(args.model, texts, args.batch_size)
    np.save(output_path, matrix)
    print(json.dumps({"ok": True, "rows": int(matrix.shape[0]), "dimension": int(matrix.shape[1])}, ensure_ascii=False))
    return 0


def run_verify(args: argparse.Namespace) -> int:
    try:
        ensure_faiss()
    except ImportError:
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

    for name in ("build", "verify", "encode-batch"):
        subparser = sub.add_parser(name)
        subparser.add_argument("--model", default=DEFAULT_MODEL)
        subparser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
        if name in ("build", "verify"):
            subparser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
            subparser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
            subparser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
            subparser.add_argument("--text-limit", type=int, default=DEFAULT_TEXT_LIMIT)
        if name == "build":
            subparser.add_argument("--worker-mode", choices=("auto", "direct", "isolated"), default="auto")
        if name == "encode-batch":
            subparser.add_argument("--input", type=Path, required=True)
            subparser.add_argument("--output", type=Path, required=True)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    command = args.command or "build"
    if command == "build":
        return run_build(args)
    if command == "verify":
        return run_verify(args)
    if command == "encode-batch":
        return run_encode_batch(args)
    parser.error(f"unknown command: {command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
