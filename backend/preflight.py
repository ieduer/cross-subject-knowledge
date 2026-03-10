#!/usr/bin/env python3
"""
Runtime asset preflight checks.

Ensures required runtime assets exist before the API process starts.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _resolve_data_asset(data_root: Path, filename: str) -> Path:
    primary = data_root / "index" / filename
    legacy = data_root / filename
    if primary.exists():
        return primary
    if legacy.exists():
        return legacy
    return primary


def main() -> int:
    project_root = Path(os.getenv("PROJECT_ROOT", Path(__file__).resolve().parents[1])).expanduser().resolve()
    default_data_root = project_root / "data"
    alt_data_root = project_root.parent / "data"
    if not (default_data_root / "index").exists() and (alt_data_root / "index").exists():
        default_data_root = alt_data_root
    data_root = Path(os.getenv("DATA_ROOT", default_data_root)).expanduser().resolve()
    state_root = Path(os.getenv("STATE_ROOT", project_root / "state")).expanduser().resolve()

    # Ensure runtime state directories are created outside code paths.
    for d in (state_root, state_root / "logs", state_root / "cache", state_root / "tmp", state_root / "batch"):
        d.mkdir(parents=True, exist_ok=True)

    required_files = [
        _resolve_data_asset(data_root, "textbook_mineru_fts.db"),
        _resolve_data_asset(data_root, "textbook_chunks.index"),
    ]
    supplemental_required = os.getenv("SUPPLEMENTAL_REQUIRED", "1").strip().lower() not in {"0", "false", "no"}
    if supplemental_required:
        required_files.extend(
            [
                _resolve_data_asset(data_root, "supplemental_textbook_pages.jsonl.gz"),
                _resolve_data_asset(data_root, "supplemental_textbook_pages.manifest.json"),
            ]
        )

    missing = [str(p) for p in required_files if not p.exists()]
    if missing:
        print("ERROR: missing runtime assets:", flush=True)
        for p in missing:
            print(f"  - {p}", flush=True)
        return 1

    print(f"Preflight OK: DATA_ROOT={data_root}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
