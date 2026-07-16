import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from backend.primary_vector_contract import (
    INDEX_VECTOR_DIGEST_ALGORITHM,
    vector_source_schema_sha256,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL = REPO_ROOT / "scripts" / "build_primary_vector_index.py"


def index_vector_digest(index_path: Path) -> str:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import faiss,sys;"
                "from backend.primary_vector_contract import faiss_index_vector_digest;"
                "print(faiss_index_vector_digest(faiss.read_index(sys.argv[1])))"
            ),
            str(index_path),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class PrimaryVectorIndexCliTests(unittest.TestCase):
    def test_cli_exposes_full_build_and_fail_closed_verify_commands(self):
        result = subprocess.run(
            [sys.executable, str(TOOL), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("build", result.stdout)
        self.assertIn("verify", result.stdout)
        self.assertIn("manifest", result.stdout)

        for command, flag in (
            ("build", "--batch-size"),
            ("manifest", "--batch-size"),
            ("verify", "--text-limit"),
        ):
            rejected = subprocess.run(
                [sys.executable, str(TOOL), command, flag, "0"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(2, rejected.returncode)
            self.assertIn("must be a positive integer", rejected.stderr)

    def test_manifest_and_verify_reject_vector_source_drift(self):
        with tempfile.TemporaryDirectory(prefix="sun-primary-index-test-") as raw_root:
            root = Path(raw_root)
            db_path = root / "textbook.db"
            index_path = root / "textbook.index"
            manifest_path = root / "textbook.manifest.json"

            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "CREATE TABLE chunks (id INTEGER PRIMARY KEY, source TEXT, text TEXT)"
                )
                conn.execute(
                    "CREATE TABLE search_logs (id INTEGER PRIMARY KEY, query TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE ai_chat_logs (id INTEGER PRIMARY KEY, prompt TEXT NOT NULL)"
                )
                conn.executemany(
                    "INSERT INTO chunks (id, source, text) VALUES (?, ?, ?)",
                    [
                        (11, "textbook", "alpha"),
                        (29, "textbook", "beta"),
                        (31, "gaokao", "excluded"),
                    ],
                )

            create_index = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import faiss,numpy as np,sys;"
                        "idx=faiss.IndexIDMap(faiss.IndexFlatIP(2));"
                        "idx.add_with_ids(np.asarray([[1,0],[0,1]],dtype='float32'),"
                        "np.asarray([11,29],dtype='int64'));"
                        "faiss.write_index(idx,sys.argv[1])"
                    ),
                    str(index_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, create_index.returncode, create_index.stderr)
            original_index_bytes = index_path.read_bytes()
            original_index_sha256 = hashlib.sha256(original_index_bytes).hexdigest()
            original_index_size = len(original_index_bytes)
            original_index_vector_digest = index_vector_digest(index_path)

            source_fingerprint = hashlib.sha256(
                (
                    json.dumps([11, "alpha"], ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                    + json.dumps([29, "beta"], ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                ).encode("utf-8")
            ).hexdigest()
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "index": {
                            "path": index_path.name,
                            "type": "IndexIDMap(IndexFlatIP)",
                            "metric": "inner_product_on_normalized_embeddings",
                            "dimension": 2,
                            "vector_rows": 2,
                            "sha256": original_index_sha256,
                            "size_bytes": original_index_size,
                            "vector_digest_algorithm": INDEX_VECTOR_DIGEST_ALGORITHM,
                            "vector_digest_sha256": original_index_vector_digest,
                        },
                        "model": {
                            "name": "test/model",
                            "text_limit_chars": 512,
                            "batch_size": 2,
                        },
                        "vector_source": {
                            "source_sql": "source != 'gaokao' AND text IS NOT NULL AND text != ''",
                            "row_count": 2,
                            "fingerprint_sha256": source_fingerprint,
                            "schema_sha256": vector_source_schema_sha256(db_path),
                        },
                        "db": {
                            "path": db_path.name,
                            "sha256": hashlib.sha256(db_path.read_bytes()).hexdigest(),
                            "size_bytes": db_path.stat().st_size,
                            "file_identity_role": "backup_audit_only",
                            "source_sql": "source != 'gaokao' AND text IS NOT NULL AND text != ''",
                            "row_count": 2,
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            common_args = [
                "--db",
                str(db_path),
                "--index",
                str(index_path),
                "--manifest",
                str(manifest_path),
                "--model",
                "test/model",
                "--text-limit",
                "512",
            ]
            verify_result = subprocess.run(
                [sys.executable, str(TOOL), "verify", *common_args],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, verify_result.returncode, verify_result.stderr)
            verified = json.loads(verify_result.stdout)
            self.assertTrue(verified["ok"])
            self.assertEqual(26, len(verified["checks"]))
            self.assertEqual(26, sum(item["ok"] for item in verified["checks"]))
            self.assertEqual(2, verified["vector_rows"])
            self.assertEqual(2, verified["dimension"])
            db_audit_check = next(
                item
                for item in verified["checks"]
                if item["name"] == "manifest_db_audit_identity"
            )
            self.assertTrue(db_audit_check["ok"])
            index_bytes_check = next(
                item
                for item in verified["checks"]
                if item["name"] == "manifest_index_bytes"
            )
            self.assertTrue(index_bytes_check["ok"])

            with sqlite3.connect(db_path) as conn:
                conn.execute("INSERT INTO search_logs (query) VALUES (?)", ("mutable-log",))
                conn.execute(
                    "INSERT INTO ai_chat_logs (prompt) VALUES (?)",
                    ("mutable-ai-log",),
                )

            mutable_log_result = subprocess.run(
                [sys.executable, str(TOOL), "verify", *common_args],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, mutable_log_result.returncode, mutable_log_result.stderr)
            mutable_log_payload = json.loads(mutable_log_result.stdout)
            self.assertTrue(mutable_log_payload["ok"])
            self.assertEqual(26, len(mutable_log_payload["checks"]))
            self.assertNotEqual(
                json.loads(manifest_path.read_text(encoding="utf-8"))["db"]["sha256"],
                mutable_log_payload["db_file_sha256"],
            )
            mutable_db_audit_check = next(
                item
                for item in mutable_log_payload["checks"]
                if item["name"] == "manifest_db_audit_identity"
            )
            vector_fingerprint_check = next(
                item
                for item in mutable_log_payload["checks"]
                if item["name"] == "vector_source_fingerprint"
            )
            self.assertTrue(mutable_db_audit_check["ok"])
            self.assertTrue(vector_fingerprint_check["ok"])

            manifest_result = subprocess.run(
                [sys.executable, str(TOOL), "manifest", *common_args, "--batch-size", "2"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, manifest_result.returncode, manifest_result.stderr)
            refreshed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                hashlib.sha256(db_path.read_bytes()).hexdigest(),
                refreshed_manifest["db"]["sha256"],
            )

            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE chunks SET text = ? WHERE id = ?", ("changed", 29))

            drift_result = subprocess.run(
                [sys.executable, str(TOOL), "verify", *common_args],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(1, drift_result.returncode, drift_result.stderr)
            drifted = json.loads(drift_result.stdout)
            self.assertFalse(drifted["ok"])
            fingerprint_check = next(
                item
                for item in drifted["checks"]
                if item["name"] == "vector_source_fingerprint"
            )
            self.assertFalse(fingerprint_check["ok"])

            rejected_reemit = subprocess.run(
                [sys.executable, str(TOOL), "manifest", *common_args, "--batch-size", "2"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(1, rejected_reemit.returncode)
            self.assertIn("run a full build", rejected_reemit.stderr)

            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE chunks SET text = ? WHERE id = ?", ("beta", 29))

            restored_source_result = subprocess.run(
                [sys.executable, str(TOOL), "verify", *common_args],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                0,
                restored_source_result.returncode,
                restored_source_result.stderr,
            )

            tamper_index = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import faiss,numpy as np,sys;"
                        "idx=faiss.IndexIDMap(faiss.IndexFlatIP(2));"
                        "idx.add_with_ids(np.asarray([[0.6,0.8],[0.8,0.6]],dtype='float32'),"
                        "np.asarray([11,29],dtype='int64'));"
                        "faiss.write_index(idx,sys.argv[1])"
                    ),
                    str(index_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, tamper_index.returncode, tamper_index.stderr)
            self.assertNotEqual(
                original_index_sha256,
                hashlib.sha256(index_path.read_bytes()).hexdigest(),
            )

            tampered_result = subprocess.run(
                [sys.executable, str(TOOL), "verify", *common_args],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(1, tampered_result.returncode, tampered_result.stderr)
            tampered_payload = json.loads(tampered_result.stdout)
            tampered_checks = {
                item["name"]: item["ok"] for item in tampered_payload["checks"]
            }
            self.assertFalse(tampered_checks["manifest_index_bytes"])
            self.assertFalse(tampered_checks["manifest_index_vector_digest"])
            self.assertTrue(tampered_checks["index_rows"])
            self.assertTrue(tampered_checks["index_ids"])

            rejected_tamper = subprocess.run(
                [sys.executable, str(TOOL), "manifest", *common_args, "--batch-size", "2"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(1, rejected_tamper.returncode)
            self.assertIn("run a full build", rejected_tamper.stderr)

            legacy_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for key in (
                "sha256",
                "size_bytes",
                "vector_digest_algorithm",
                "vector_digest_sha256",
            ):
                legacy_manifest["index"].pop(key, None)
            manifest_path.write_text(
                json.dumps(legacy_manifest, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            naked_legacy_upgrade = subprocess.run(
                [
                    sys.executable,
                    str(TOOL),
                    "manifest",
                    *common_args,
                    "--batch-size",
                    "2",
                    "--allow-legacy-index-metadata-upgrade",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(1, naked_legacy_upgrade.returncode)
            self.assertIn("anchors are missing or do not match", naked_legacy_upgrade.stderr)

            anchored_legacy_upgrade = subprocess.run(
                [
                    sys.executable,
                    str(TOOL),
                    "manifest",
                    *common_args,
                    "--batch-size",
                    "2",
                    "--allow-legacy-index-metadata-upgrade",
                    "--expected-index-sha256",
                    original_index_sha256,
                    "--expected-vector-digest-sha256",
                    original_index_vector_digest,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(1, anchored_legacy_upgrade.returncode)
            self.assertIn("anchors are missing or do not match", anchored_legacy_upgrade.stderr)

    def test_release_manifest_supports_external_data_roots(self):
        module_path = REPO_ROOT / "scripts" / "build_release_manifest.py"
        spec = importlib.util.spec_from_file_location("release_manifest_builder", module_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        original_workspace_root = module.WORKSPACE_ROOT
        module.WORKSPACE_ROOT = Path("/workspace-root")
        try:
            with tempfile.TemporaryDirectory(prefix="sun-external-runtime-") as raw_root:
                external_path = Path(raw_root).resolve() / "index" / "asset.db"
                self.assertEqual(str(external_path), module.runtime_source_path(external_path))

            workspace_path = Path("/workspace-root/runtime-test/index/asset.db")
            self.assertEqual(
                "runtime-test/index/asset.db",
                module.runtime_source_path(workspace_path),
            )
        finally:
            module.WORKSPACE_ROOT = original_workspace_root

    def test_release_manifest_ignores_logs_but_rejects_content_drift(self):
        builder_path = REPO_ROOT / "scripts" / "build_release_manifest.py"
        verifier_path = REPO_ROOT / "scripts" / "verify_release_manifest.py"
        builder_spec = importlib.util.spec_from_file_location(
            "release_manifest_mutable_builder",
            builder_path,
        )
        verifier_spec = importlib.util.spec_from_file_location(
            "release_manifest_mutable_verifier",
            verifier_path,
        )
        self.assertIsNotNone(builder_spec)
        self.assertIsNotNone(builder_spec.loader)
        self.assertIsNotNone(verifier_spec)
        self.assertIsNotNone(verifier_spec.loader)
        builder = importlib.util.module_from_spec(builder_spec)
        verifier = importlib.util.module_from_spec(verifier_spec)
        builder_spec.loader.exec_module(builder)
        verifier_spec.loader.exec_module(verifier)
        builder.TEXTBOOK_DB_FINGERPRINT_TABLES = ("chunks",)
        builder.TEXTBOOK_DB_FTS_COUNT_TABLES = ()
        verifier.TEXTBOOK_DB_FINGERPRINT_TABLES = ("chunks",)
        verifier.TEXTBOOK_DB_FTS_COUNT_TABLES = ()

        with tempfile.TemporaryDirectory(prefix="sun-release-db-test-") as raw_root:
            runtime_root = Path(raw_root)
            db_path = runtime_root / "data" / "index" / "textbook.db"
            db_path.parent.mkdir(parents=True)
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "CREATE TABLE chunks (id INTEGER PRIMARY KEY, source TEXT, text TEXT)"
                )
                conn.execute(
                    "CREATE TABLE search_logs (id INTEGER PRIMARY KEY, query TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE ai_chat_logs (id INTEGER PRIMARY KEY, prompt TEXT NOT NULL)"
                )
                conn.executemany(
                    "INSERT INTO chunks (id, source, text) VALUES (?, ?, ?)",
                    [(1, "textbook", "alpha"), (2, "textbook", "beta")],
                )

            original_sha256 = hashlib.sha256(db_path.read_bytes()).hexdigest()
            entry = {
                "kind": "runtime",
                "logical_path": "data/index/textbook.db",
                "sha256": original_sha256,
                "size_bytes": db_path.stat().st_size,
                "file_identity_role": "backup_audit_only",
                "runtime_identity": builder.textbook_db_runtime_identity(db_path),
            }
            self.assertEqual(
                [],
                verifier.verify_entries([entry], runtime_root, "runtime"),
            )

            with sqlite3.connect(db_path) as conn:
                conn.execute("INSERT INTO search_logs (query) VALUES (?)", ("query",))
                conn.execute(
                    "INSERT INTO ai_chat_logs (prompt) VALUES (?)",
                    ("prompt",),
                )
            self.assertNotEqual(
                original_sha256,
                hashlib.sha256(db_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                [],
                verifier.verify_entries([entry], runtime_root, "runtime"),
            )

            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE chunks SET text = ? WHERE id = ?", ("changed", 2))
            errors = verifier.verify_entries([entry], runtime_root, "runtime")
            self.assertEqual(1, len(errors))
            self.assertIn("runtime identity mismatch", errors[0])

    def test_release_and_maintenance_surfaces_include_the_tool(self):
        builder = (REPO_ROOT / "scripts" / "build_release_manifest.py").read_text(encoding="utf-8")
        stager = (REPO_ROOT / "scripts" / "stage_clean_release.py").read_text(encoding="utf-8")
        manual = (REPO_ROOT / "docs" / "MAINTENANCE_MANUAL.md").read_text(encoding="utf-8")
        logical_path = "scripts/build_primary_vector_index.py"
        contract_path = "backend/primary_vector_contract.py"
        self.assertIn(logical_path, builder)
        self.assertIn(logical_path, stager)
        self.assertIn(contract_path, builder)
        self.assertIn(contract_path, stager)
        self.assertIn(f"python3 {logical_path} build", manual)
        self.assertIn(f"python3 {logical_path} verify", manual)

    def test_release_tools_accept_the_standalone_github_checkout(self):
        for script_name in ("build_release_manifest.py", "stage_clean_release.py"):
            script_path = REPO_ROOT / "scripts" / script_name
            check = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import importlib.util,sys;"
                        "spec=importlib.util.spec_from_file_location('release_tool',sys.argv[1]);"
                        "module=importlib.util.module_from_spec(spec);"
                        "spec.loader.exec_module(module);"
                        "module.verify_textbook_config_sync()"
                    ),
                    str(script_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, check.returncode, check.stderr)


if __name__ == "__main__":
    unittest.main()
