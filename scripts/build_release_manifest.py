#!/Users/ylsuen/.venv/bin/python
"""Build a machine-readable release manifest for code, runtime data, and page mappings."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PLATFORM_ROOT.parent
DATA_INDEX = WORKSPACE_ROOT / "data" / "index"
OUTPUT_PATH = PLATFORM_ROOT / "release_manifest.json"
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

SOURCE_ASSETS = (
    "Dockerfile",
    ".dockerignore",
    "requirements.runtime.txt",
    "backend/main.py",
    "backend/ai_guard.py",
    "backend/library.py",
    "backend/entrypoint.sh",
    "backend/preflight.py",
    "backend/sync_db.py",
    "backend/textbook_config.py",
    "backend/textbook_classics_manifest.json",
    "backend/textbook_version_manifest.json",
    "backend/xuci_single_char_index.json",
    "backend/supplemental_textbook_pages.jsonl.gz",
    "backend/supplemental_textbook_pages.manifest.json",
    "frontend/index.html",
    "frontend/dict.html",
    "frontend/chuzhong.html",
    "frontend/chuzhong-dict.html",
    "frontend/assets/ai-client.js",
    "frontend/assets/app.js",
    "frontend/assets/style.css",
    "frontend/assets/background-mx.jpg",
    "frontend/assets/dict.js",
    "frontend/assets/dict.css",
    "frontend/assets/library.css",
    "frontend/assets/library.js",
    "frontend/assets/version.json",
    "frontend/assets/pages/book_map.json",
    "scripts/deploy_vps.sh",
    "scripts/stage_clean_release.py",
    "scripts/build_release_manifest.py",
    "scripts/verify_release_manifest.py",
)

# Whole directories whose every file is a source asset (vendored PDF.js incl. VENDOR.json).
SOURCE_TREES = (
    "frontend/vendor/pdfjs",
)


def source_asset_paths() -> list[str]:
    paths = list(SOURCE_ASSETS)
    for relative_dir in SOURCE_TREES:
        base = PLATFORM_ROOT / relative_dir
        if not base.is_dir():
            raise FileNotFoundError(f"Missing required source directory: {base}")
        for path in sorted(base.rglob("*")):
            if path.is_file():
                rel = path.relative_to(PLATFORM_ROOT).as_posix()
                if rel not in paths:
                    paths.append(rel)
    return paths


RUNTIME_ASSETS = (
    ("textbook_mineru_fts.db", DATA_INDEX / "textbook_mineru_fts.db"),
    ("textbook_chunks.index", DATA_INDEX / "textbook_chunks.index"),
    ("textbook_chunks.manifest.json", DATA_INDEX / "textbook_chunks.manifest.json"),
    (
        "supplemental_textbook_pages.jsonl.gz",
        PLATFORM_ROOT / "backend" / "supplemental_textbook_pages.jsonl.gz",
    ),
    (
        "supplemental_textbook_pages.manifest.json",
        PLATFORM_ROOT / "backend" / "supplemental_textbook_pages.manifest.json",
    ),
    ("supplemental_textbook_pages.index", DATA_INDEX / "supplemental_textbook_pages.index"),
    (
        "supplemental_textbook_pages.vector.manifest.json",
        DATA_INDEX / "supplemental_textbook_pages.vector.manifest.json",
    ),
    ("dict_exam_xuci.json", DATA_INDEX / "dict_exam_xuci.json"),
    ("dict_exam_shici.json", DATA_INDEX / "dict_exam_shici.json"),
    ("dict_exam_xuci_details.json", DATA_INDEX / "dict_exam_xuci_details.json"),
    ("dict_moe_revised.db", DATA_INDEX / "dict_moe_revised.db"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build release_manifest.json for textbook-knowledge.")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument(
        "--reuse-runtime-from",
        type=Path,
        help=(
            "Rebuild only the source-asset half and carry runtime_assets / runtime_facts over, unchanged, "
            "from this existing manifest. For code-only releases prepared on a machine that does not hold "
            "the runtime data (data/index). The carried entries are still verified against the VPS by "
            "deploy_vps.sh before cutover. Not valid when any runtime data file changed."
        ),
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_entry(kind: str, logical_path: str, actual_path: Path, source_path: str | None = None) -> dict[str, object]:
    if not actual_path.exists():
        raise FileNotFoundError(f"Missing required {kind} asset: {actual_path}")
    stat = actual_path.stat()
    payload = {
        "kind": kind,
        "logical_path": logical_path,
        "size": stat.st_size,
        "sha256": sha256_file(actual_path),
    }
    if source_path:
        payload["source_path"] = source_path
    return payload


def sqlite_ordered_table_hash(conn: sqlite3.Connection, table_name: str) -> dict[str, object]:
    columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()]
    if not columns:
        return {"row_count": 0, "sha256": hashlib.sha256(f"{table_name}\n".encode("utf-8")).hexdigest()}
    order_clause = ", ".join(f'"{column}"' for column in columns)
    digest = hashlib.sha256()
    row_count = 0
    query = f'SELECT * FROM "{table_name}" ORDER BY {order_clause}'
    cursor = conn.execute(query)
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


def git_head_sha() -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "-C", str(PLATFORM_ROOT), "rev-parse", "HEAD"],
                text=True,
            )
            .strip()
        )
    except Exception:
        return ""


def load_frontend_version() -> str:
    payload = json.loads((PLATFORM_ROOT / "frontend" / "assets" / "version.json").read_text(encoding="utf-8"))
    return str(payload.get("frontend_refactor_version") or "").strip()


def book_map_summary() -> dict[str, object]:
    payload = json.loads((PLATFORM_ROOT / "frontend" / "assets" / "pages" / "book_map.json").read_text(encoding="utf-8"))
    return {
        "book_count": len(payload),
        "required_r2_prefixes": [
            "pages/",
            "pages/dict_xuci/",
            "pages/dict_changyong/",
        ],
    }


def sqlite_row_count(path: Path, query: str) -> int | None:
    if not path.exists():
        return None
    conn = connect_textbook_db(path)
    try:
        value = conn.execute(query).fetchone()
        return int(value[0]) if value and value[0] is not None else None
    finally:
        conn.close()


def verify_textbook_config_sync() -> None:
    """Fail-fast if scripts/textbook_config.py and platform/backend/textbook_config.py diverge."""
    import filecmp
    canonical = WORKSPACE_ROOT / "scripts" / "textbook_config.py"
    synced = PLATFORM_ROOT / "backend" / "textbook_config.py"
    if not canonical.exists():
        raise FileNotFoundError(f"Missing canonical textbook_config.py: {canonical}")
    if not synced.exists():
        raise FileNotFoundError(
            f"Missing synced textbook_config.py: {synced}\n"
            f"Run: scripts/sync_shared_config.sh"
        )
    if not filecmp.cmp(canonical, synced, shallow=False):
        raise RuntimeError(
            f"FATAL: {synced} is out of sync with {canonical}.\n"
            f"Run: scripts/sync_shared_config.sh"
        )


def build_manifest(reuse_runtime_from: Path | None = None) -> dict[str, object]:
    source_assets = [
        file_entry("source", rel_path, PLATFORM_ROOT / rel_path)
        for rel_path in source_asset_paths()
    ]
    if reuse_runtime_from is not None:
        return build_manifest_with_carried_runtime(source_assets, reuse_runtime_from)
    runtime_assets = []
    for logical_name, actual_path in RUNTIME_ASSETS:
        entry = file_entry(
            "runtime",
            f"data/index/{logical_name}",
            actual_path,
            source_path=str(actual_path.relative_to(WORKSPACE_ROOT)),
        )
        if logical_name == "textbook_mineru_fts.db":
            entry["runtime_identity"] = textbook_db_runtime_identity(actual_path)
        runtime_assets.append(entry)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_head": git_head_sha(),
        "frontend_version": load_frontend_version(),
        "source_of_truth": {
            "local_workspace": "authoritative",
            "github": "authoritative_for_code_only_after_push",
            "vps": "authoritative_for_runtime_large_assets_after_sync",
            "r2": "authoritative_for_page_images_after_sync",
        },
        "source_assets": source_assets,
        "runtime_assets": runtime_assets,
        "page_mapping": book_map_summary(),
        "runtime_facts": {
            "textbook_mineru_rows": sqlite_row_count(
                DATA_INDEX / "textbook_mineru_fts.db",
                "select count(*) from chunks",
            ),
            "dict_moe_revised_rows": sqlite_row_count(
                DATA_INDEX / "dict_moe_revised.db",
                "select count(*) from entries",
            ),
            "dict_moe_revised_headwords": sqlite_row_count(
                DATA_INDEX / "dict_moe_revised.db",
                "select count(distinct headword) from entries",
            ),
        },
        "release_rules": [
            "code changes must be pushed to GitHub before or together with deployment",
            "runtime data changes must be synchronized to VPS before production cutover",
            "page-image changes must be synchronized to R2 before production cutover",
            "deploy_vps.sh must verify this manifest before cutover",
        ],
    }


def build_manifest_with_carried_runtime(source_assets: list[dict[str, object]], previous_path: Path) -> dict[str, object]:
    previous = json.loads(previous_path.read_text(encoding="utf-8"))
    runtime_assets = previous.get("runtime_assets")
    if previous.get("schema_version") != 1 or not isinstance(runtime_assets, list) or not runtime_assets:
        raise RuntimeError(f"{previous_path} has no runtime_assets to carry over")
    expected = {f"data/index/{name}" for name, _ in RUNTIME_ASSETS}
    carried = {str(entry.get("logical_path")) for entry in runtime_assets}
    if carried != expected:
        raise RuntimeError(
            "runtime asset list of the previous manifest differs from RUNTIME_ASSETS; "
            "run without --reuse-runtime-from on a machine that holds data/index"
        )
    # The two bundled supplemental files are both source and runtime assets: they must not have changed.
    by_path = {str(entry["logical_path"]): entry for entry in source_assets}
    for entry in runtime_assets:
        source_path = str(entry.get("source_path") or "")
        remainder = source_path.split("/", 1)[1] if "/" in source_path else ""
        twin = by_path.get(remainder) if remainder.startswith("backend/") else None
        if twin is not None and twin["sha256"] != entry.get("sha256"):
            raise RuntimeError(f"bundled runtime file changed since the previous manifest: {source_path}")
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_head": git_head_sha(),
        "frontend_version": load_frontend_version(),
        "source_of_truth": previous.get("source_of_truth"),
        "source_assets": source_assets,
        "runtime_assets": runtime_assets,
        "runtime_assets_carried_from": {
            "generated_at": previous.get("generated_at"),
            "git_head": previous.get("git_head"),
            "note": "runtime half not re-measured in this build; deploy_vps.sh verifies it on the VPS",
        },
        "page_mapping": book_map_summary(),
        "runtime_facts": previous.get("runtime_facts"),
        "release_rules": previous.get("release_rules"),
    }
    # Notes about how the runtime half was measured (e.g. runtime_assets_measured_from, with
    # hand-recorded on-host hashes) travel with the entries they describe.
    for key, value in previous.items():
        if key.startswith("runtime_assets_") and key != "runtime_assets_carried_from":
            manifest[key] = value
    return manifest


def main() -> None:
    args = parse_args()
    if args.reuse_runtime_from is None:
        verify_textbook_config_sync()
    else:
        # Without the canonical workspace copy the sync check cannot run; require instead that
        # backend/textbook_config.py is byte-identical to the one the previous manifest recorded.
        previous = json.loads(args.reuse_runtime_from.read_text(encoding="utf-8"))
        recorded = {str(e.get("logical_path")): e.get("sha256") for e in previous.get("source_assets") or []}
        canonical = WORKSPACE_ROOT / "scripts" / "textbook_config.py"
        if canonical.exists():
            verify_textbook_config_sync()
        elif sha256_file(PLATFORM_ROOT / "backend" / "textbook_config.py") != recorded.get("backend/textbook_config.py"):
            raise RuntimeError("backend/textbook_config.py changed and the canonical copy is not available to compare")
    manifest = build_manifest(args.reuse_runtime_from)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(args.output)


if __name__ == "__main__":
    main()
