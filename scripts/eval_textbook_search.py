#!/usr/bin/env python3
"""
Run a lightweight regression suite against textbook hybrid search.

Examples:
  /Users/ylsuen/.venv/bin/python scripts/eval_textbook_search.py
  /Users/ylsuen/.venv/bin/python scripts/eval_textbook_search.py --only crystal_definition
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import main as backend  # noqa: E402


DEFAULT_CASES = Path(__file__).with_name("textbook_search_regression_cases.json")


def load_cases(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("regression cases must be a list")
    return [item for item in payload if isinstance(item, dict)]


def evaluate_case(case: dict) -> dict:
    query = str(case.get("query") or "").strip()
    scope_subject = str(case.get("scope_subject") or "").strip() or None
    book_key = str(case.get("book_key") or "").strip() or None
    top_k = max(1, int(case.get("top_k") or 5))
    require_precision_mode = bool(case.get("require_precision_mode"))
    expected_subjects = {str(item).strip() for item in (case.get("expect_subjects") or []) if str(item).strip()}
    expected_fragments = [str(item).strip() for item in (case.get("expect_any_substrings") or []) if str(item).strip()]

    con = backend.get_db()
    try:
        analysis = backend._analyze_search_query(con, query, scope_subject=scope_subject, book_key=book_key)
        rows, meta = backend._collect_hybrid_search_rows(
            con,
            query,
            analysis,
            scope_subject=scope_subject,
            book_key=book_key,
            candidate_limit=max(40, top_k * 10),
        )
    finally:
        con.close()

    top_rows = rows[:top_k]
    top_subjects = [row.get("subject") or "" for row in top_rows]
    evidence_blobs = [
        "\n".join(
            part
            for part in (
                row.get("title") or "",
                row.get("snippet") or "",
                row.get("text") or "",
            )
            if part
        )
        for row in top_rows
    ]

    subject_ok = True
    if expected_subjects:
        subject_ok = any(subject in expected_subjects for subject in top_subjects)

    fragment_ok = True
    if expected_fragments:
        fragment_ok = any(
            fragment in blob
            for fragment in expected_fragments
            for blob in evidence_blobs
        )

    precision_ok = True
    if require_precision_mode:
        precision_ok = bool(meta.get("precision_mode"))

    passed = bool(top_rows) and subject_ok and fragment_ok and precision_ok
    return {
        "name": case.get("name") or query,
        "query": query,
        "ok": passed,
        "precision_mode": bool(meta.get("precision_mode")),
        "query_intent": meta.get("query_intent"),
        "candidate_count": int(meta.get("candidate_count") or 0),
        "top_subjects": top_subjects,
        "top_titles": [row.get("title") or "" for row in top_rows],
        "top_snippets": [row.get("snippet") or "" for row in top_rows],
        "checks": {
            "has_results": bool(top_rows),
            "subject_ok": subject_ok,
            "fragment_ok": fragment_ok,
            "precision_ok": precision_ok,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run regression checks against textbook hybrid search.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--only", help="Only run a single named case")
    args = parser.parse_args()

    cases = load_cases(args.cases.expanduser().resolve())
    if args.only:
        cases = [case for case in cases if case.get("name") == args.only]
        if not cases:
            print(json.dumps({"ok": False, "error": f"case not found: {args.only}"}, ensure_ascii=False))
            return 2

    results = [evaluate_case(case) for case in cases]
    ok = all(item.get("ok") for item in results)
    print(json.dumps({"ok": ok, "cases": results}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
