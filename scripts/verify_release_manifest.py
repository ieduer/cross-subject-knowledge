#!/Users/ylsuen/.venv/bin/python
"""Verify source and runtime assets against release_manifest.json."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PLATFORM_ROOT / "release_manifest.json"
DEFAULT_WORKSPACE_ROOT = PLATFORM_ROOT.parent


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
