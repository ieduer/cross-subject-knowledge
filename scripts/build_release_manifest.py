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

SOURCE_ASSETS = (
    "Dockerfile",
    ".dockerignore",
    "requirements.runtime.txt",
    "backend/main.py",
    "backend/entrypoint.sh",
    "backend/preflight.py",
    "backend/sync_db.py",
    "backend/textbook_classics_manifest.json",
    "backend/textbook_version_manifest.json",
    "backend/xuci_single_char_index.json",
    "backend/supplemental_textbook_pages.jsonl.gz",
    "backend/supplemental_textbook_pages.manifest.json",
    "frontend/index.html",
    "frontend/dict.html",
    "frontend/assets/app.js",
    "frontend/assets/style.css",
    "frontend/assets/dict.js",
    "frontend/assets/dict.css",
    "frontend/assets/version.json",
    "frontend/assets/pages/book_map.json",
    "scripts/deploy_vps.sh",
    "scripts/stage_clean_release.py",
    "scripts/build_release_manifest.py",
    "scripts/verify_release_manifest.py",
)

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
    conn = sqlite3.connect(path)
    try:
        value = conn.execute(query).fetchone()
        return int(value[0]) if value and value[0] is not None else None
    finally:
        conn.close()


def build_manifest() -> dict[str, object]:
    source_assets = [
        file_entry("source", rel_path, PLATFORM_ROOT / rel_path)
        for rel_path in SOURCE_ASSETS
    ]
    runtime_assets = []
    for logical_name, actual_path in RUNTIME_ASSETS:
        runtime_assets.append(
            file_entry(
                "runtime",
                f"data/index/{logical_name}",
                actual_path,
                source_path=str(actual_path.relative_to(WORKSPACE_ROOT)),
            )
        )
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


def main() -> None:
    args = parse_args()
    manifest = build_manifest()
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(args.output)


if __name__ == "__main__":
    main()
