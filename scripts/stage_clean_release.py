#!/Users/ylsuen/.venv/bin/python
"""Stage a minimal clean release bundle for textbook-knowledge.

This script exists to stop ad-hoc VPS runtime repos from becoming deploy sources.
It stages the current runtime-relevant files into a clean directory and writes a
machine-readable manifest so manual releases can be verified before cutover.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_NAME = "release_manifest.json"

RELEASE_FILES = (
    ".dockerignore",
    "Dockerfile",
    "release_manifest.json",
    "requirements.runtime.txt",
    "backend/entrypoint.sh",
    "backend/main.py",
    "backend/preflight.py",
    "backend/supplemental_textbook_pages.jsonl.gz",
    "backend/supplemental_textbook_pages.manifest.json",
    "backend/sync_db.py",
    "backend/textbook_config.py",
    "backend/textbook_classics_manifest.json",
    "backend/textbook_version_manifest.json",
    "backend/xuci_single_char_index.json",
    "frontend/index.html",
    "frontend/dict.html",
    "frontend/chuzhong.html",
    "frontend/chuzhong-dict.html",
    "frontend/assets/app.js",
    "frontend/assets/style.css",
    "frontend/assets/dict.js",
    "frontend/assets/dict.css",
    "frontend/assets/version.json",
    "frontend/assets/pages/book_map.json",
    "scripts/build_release_manifest.py",
    "scripts/deploy_vps.sh",
    "scripts/verify_release_manifest.py",
    "scripts/stage_clean_release.py",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage a clean runtime release bundle.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory to populate with the clean release tree.",
    )
    parser.add_argument(
        "--archive",
        type=Path,
        help="Optional .tar.gz archive path to write after staging the directory.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing an existing output directory or archive.",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_clean_path(path: Path, overwrite: bool) -> None:
    if not path.exists():
        return
    if not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing path: {path}")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def stage_file(relative_path: str, output_dir: Path) -> dict[str, object]:
    source = REPO_ROOT / relative_path
    if not source.exists():
        raise FileNotFoundError(f"Missing required release file: {source}")
    destination = output_dir / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    stat = source.stat()
    return {
        "path": relative_path,
        "size": stat.st_size,
        "sha256": sha256_file(source),
    }


def load_frontend_version() -> str:
    version_path = REPO_ROOT / "frontend" / "assets" / "version.json"
    payload = json.loads(version_path.read_text(encoding="utf-8"))
    return str(payload.get("frontend_refactor_version") or "").strip()


def git_commit_sha() -> str:
    head = REPO_ROOT / ".git" / "HEAD"
    if not head.exists():
        return ""
    ref = head.read_text(encoding="utf-8").strip()
    if ref.startswith("ref: "):
        ref_path = REPO_ROOT / ".git" / ref[5:]
        if ref_path.exists():
            return ref_path.read_text(encoding="utf-8").strip()
    return ref


def build_manifest(staged_files: list[dict[str, object]], output_dir: Path) -> Path:
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(REPO_ROOT),
        "git_commit_sha": git_commit_sha(),
        "frontend_version": load_frontend_version(),
        "file_count": len(staged_files),
        "required_files": staged_files,
        "guardrails": {
            "manual_release_source": "clean_release_bundle_only",
            "requires_book_map": True,
            "forbid_runtime_repo_as_source": True,
        },
    }
    manifest_path = output_dir / DEFAULT_MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def write_archive(source_dir: Path, archive_path: Path, overwrite: bool) -> None:
    ensure_clean_path(archive_path, overwrite)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w:gz"
    with tarfile.open(archive_path, mode) as tar:
        tar.add(source_dir, arcname=".")


def verify_textbook_config_sync() -> None:
    """Fail-fast if scripts/textbook_config.py and platform/backend/textbook_config.py diverge."""
    import filecmp
    workspace_root = REPO_ROOT.parent
    canonical = workspace_root / "scripts" / "textbook_config.py"
    synced = REPO_ROOT / "backend" / "textbook_config.py"
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


def main() -> None:
    args = parse_args()
    verify_textbook_config_sync()
    output_dir = args.output_dir.resolve()
    ensure_clean_path(output_dir, args.overwrite)
    output_dir.mkdir(parents=True, exist_ok=True)

    staged_files: list[dict[str, object]] = []
    for relative_path in RELEASE_FILES:
        staged_files.append(stage_file(relative_path, output_dir))

    manifest_path = build_manifest(staged_files, output_dir)

    if args.archive:
        write_archive(output_dir, args.archive.resolve(), args.overwrite)

    print(f"staged_release={output_dir}")
    print(f"manifest={manifest_path}")
    if args.archive:
        print(f"archive={args.archive.resolve()}")
    print(f"frontend_version={load_frontend_version()}")
    print(f"file_count={len(staged_files)}")


if __name__ == "__main__":
    main()
