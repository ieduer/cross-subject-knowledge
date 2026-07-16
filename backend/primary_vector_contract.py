"""Shared fail-closed identity helpers for the primary textbook FAISS pair."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Iterable


INDEX_VECTOR_DIGEST_ALGORITHM = "faiss-idmap-flatip-f32-v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _connect_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def sqlite_schema_sha256(path: Path, table_names: Iterable[str]) -> str:
    conn = _connect_read_only(path)
    try:
        tables: list[dict[str, object]] = []
        for table_name in sorted(set(table_names)):
            row = conn.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
            if row is None:
                tables.append({"name": table_name, "missing": True})
                continue
            tables.append(
                {
                    "type": row[0],
                    "name": row[1],
                    "sql": row[2],
                    "columns": conn.execute(
                        f'PRAGMA table_info("{table_name}")'
                    ).fetchall(),
                }
            )
    finally:
        conn.close()
    payload = json.dumps(
        tables,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def vector_source_schema_sha256(path: Path) -> str:
    return sqlite_schema_sha256(path, ("chunks",))


def faiss_index_ids(index: object) -> list[int] | None:
    if not hasattr(index, "id_map"):
        return None
    import faiss

    return [int(item) for item in faiss.vector_to_array(index.id_map)]


def faiss_index_inner_type(index: object) -> str | None:
    if not hasattr(index, "index"):
        return None
    import faiss

    return type(faiss.downcast_index(index.index)).__name__


def faiss_index_vector_digest(index: object, batch_size: int = 256) -> str:
    import faiss
    import numpy as np

    ids = faiss_index_ids(index)
    if ids is None:
        raise ValueError("FAISS index has no stable ID map")
    if not hasattr(index, "index"):
        raise ValueError("FAISS index has no reconstructable inner index")
    inner = faiss.downcast_index(index.index)
    if type(inner).__name__ != "IndexFlatIP":
        raise ValueError(f"unsupported FAISS inner index: {type(inner).__name__}")

    metadata = {
        "algorithm": INDEX_VECTOR_DIGEST_ALGORITHM,
        "dimension": int(index.d),
        "metric_type": int(index.metric_type),
        "rows": int(index.ntotal),
    }
    digest = hashlib.sha256()
    digest.update(
        json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    digest.update(b"\n")

    for start in range(0, int(index.ntotal), batch_size):
        count = min(batch_size, int(index.ntotal) - start)
        matrix = np.asarray(
            inner.reconstruct_n(start, count),
            dtype="<f4",
            order="C",
        )
        for offset in range(count):
            digest.update(int(ids[start + offset]).to_bytes(8, "little", signed=True))
            digest.update(matrix[offset].tobytes(order="C"))
    return digest.hexdigest()
