#!/Users/ylsuen/.venv/bin/python
"""Verify source and runtime assets against release_manifest.json."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PLATFORM_ROOT / "release_manifest.json"
DEFAULT_WORKSPACE_ROOT = PLATFORM_ROOT.parent
TEXTBOOK_DB_MUTABLE_TABLES = frozenset({
    "search_logs",
    "ai_chat_logs",
    "ai_batch_ingest",
    "ai_batch_jobs",
    "sqlite_sequence",
})
TEXTBOOK_DB_FINGERPRINT_TABLES = (
    "chunks",
    "ai_summaries",
    "ai_explanations",
    "ai_synonyms",
    "concept_map",
    "cross_subject_map",
    "curated_keywords",
    "keyword_counts",
    "ai_gaokao_links",
    "ai_relations",
)
TEXTBOOK_DB_FTS_COUNT_TABLES = (
    "chunks_fts",
    "chunks_fts_data",
    "chunks_fts_idx",
    "chunks_fts_docsize",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify source/runtime assets against a release manifest.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--source-root", type=Path, default=PLATFORM_ROOT)
    parser.add_argument("--workspace-root", type=Path, default=DEFAULT_WORKSPACE_ROOT)
    parser.add_argument("--runtime-root", type=Path, help="Runtime root containing data/index on VPS.")
    parser.add_argument(
        "--check",
        action="append",
        choices=("source", "runtime", "runtime-source"),
        help="Which asset groups to verify. Defaults to source + runtime-source locally, or source + runtime when runtime-root is set.",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_ordered_table_hash(conn: sqlite3.Connection, table_name: str) -> dict[str, object]:
    columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()]
    if not columns:
        return {"row_count": 0, "sha256": hashlib.sha256(f"{table_name}\n".encode("utf-8")).hexdigest()}
    order_clause = ", ".join(f'"{column}"' for column in columns)
    digest = hashlib.sha256()
    row_count = 0
    cursor = conn.execute(f'SELECT * FROM "{table_name}" ORDER BY {order_clause}')
    for row in cursor:
        digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
        digest.update(b"\n")
        row_count += 1
    return {"row_count": row_count, "sha256": digest.hexdigest()}


def connect_textbook_db(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA query_only = ON")
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        return conn
    except sqlite3.OperationalError as exc:
        try:
            conn.close()
        except Exception:
            pass
        if "unable to open database file" not in str(exc):
            raise
        uri = f"file:{path}?mode=ro&immutable=1"
        return sqlite3.connect(uri, uri=True)


def textbook_db_runtime_identity(path: Path) -> dict[str, object]:
    conn = connect_textbook_db(path)
    try:
        payload: dict[str, object] = {
            "type": "sqlite_textbook_runtime_identity_v1",
            "mutable_tables": sorted(TEXTBOOK_DB_MUTABLE_TABLES),
            "content_tables": {},
            "fts_shadow_counts": {},
            "integrity_check": conn.execute("PRAGMA integrity_check").fetchone()[0],
        }
        content_tables = payload["content_tables"]
        assert isinstance(content_tables, dict)
        for table_name in TEXTBOOK_DB_FINGERPRINT_TABLES:
            content_tables[table_name] = sqlite_ordered_table_hash(conn, table_name)
        fts_counts = payload["fts_shadow_counts"]
        assert isinstance(fts_counts, dict)
        for table_name in TEXTBOOK_DB_FTS_COUNT_TABLES:
            fts_counts[table_name] = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
        return payload
    finally:
        conn.close()


def verify_entries(entries: list[dict], root: Path, expected_kind: str, *, use_source_path: bool = False) -> list[str]:
    errors: list[str] = []
    for entry in entries:
        if entry.get("kind") != expected_kind:
            continue
        relative_path = str(entry.get("source_path") if use_source_path else entry["logical_path"])
        actual = root / relative_path
        if not actual.exists():
            label = f"{expected_kind}{'-source' if use_source_path else ''}"
            errors.append(f"missing {label} asset: {actual}")
            continue
        runtime_identity = entry.get("runtime_identity")
        if expected_kind == "runtime" and isinstance(runtime_identity, dict):
            actual_identity = textbook_db_runtime_identity(actual)
            if actual_identity != runtime_identity:
                errors.append(
                    f"runtime identity mismatch for {expected_kind}{'-source' if use_source_path else ''} asset {relative_path}"
                )
            continue
        actual_sha = sha256_file(actual)
        expected_sha = str(entry.get("sha256") or "")
        if actual_sha != expected_sha:
            errors.append(
                f"sha mismatch for {expected_kind}{'-source' if use_source_path else ''} asset {relative_path}: expected {expected_sha}, got {actual_sha}"
            )
    return errors


def main() -> None:
    args = parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    checks = args.check or (["source", "runtime"] if args.runtime_root else ["source", "runtime-source"])

    errors: list[str] = []
    source_root = args.source_root.resolve()
    workspace_root = args.workspace_root.resolve()
    runtime_root = args.runtime_root.resolve() if args.runtime_root else None

    if "source" in checks:
        errors.extend(verify_entries(manifest.get("source_assets") or [], source_root, "source"))

    if "runtime-source" in checks:
        errors.extend(
            verify_entries(
                manifest.get("runtime_assets") or [],
                workspace_root,
                "runtime",
                use_source_path=True,
            )
        )

    if "runtime" in checks:
        if runtime_root is None:
            errors.append("runtime verification requested but --runtime-root was not provided")
        else:
            errors.extend(verify_entries(manifest.get("runtime_assets") or [], runtime_root, "runtime"))

    if errors:
        for item in errors:
            print(f"ERROR: {item}", file=sys.stderr)
        sys.exit(1)

    print(
        json.dumps(
            {
                "ok": True,
                "manifest": str(args.manifest.resolve()),
                "checks": checks,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
