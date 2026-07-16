import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
_runtime_dir = tempfile.TemporaryDirectory(prefix="sun-contract-tests-")
_data_root = Path(_runtime_dir.name) / "data"
(_data_root / "index").mkdir(parents=True)
os.environ["DATA_ROOT"] = str(_data_root)
os.environ["STATE_ROOT"] = str(Path(_runtime_dir.name) / "state")
sys.path.insert(0, str(BACKEND_ROOT))

# Business-contract tests do not exercise vector search. Prevent importing the
# heavyweight local ML runtime while keeping the production import path intact.
sys.modules["faiss"] = None
sys.modules["jieba"] = None

import main as backend  # noqa: E402


class SearchContractTests(unittest.TestCase):
    def test_single_hanzi_does_not_expand_to_long_substring_alias(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(
            """
            CREATE TABLE ai_synonyms (term TEXT NOT NULL, synonyms TEXT NOT NULL);
            CREATE TABLE chunks (
                id INTEGER PRIMARY KEY,
                source TEXT,
                text TEXT,
                subject TEXT,
                book_key TEXT
            );
            """
        )
        con.execute(
            "INSERT INTO ai_synonyms (term, synonyms) VALUES (?, ?)",
            ("洋务运动", '{"aliases":["师夷长技以制夷"]}'),
        )

        analysis = backend._analyze_search_query(con, "以", scope_subject="语文")
        plan = backend._build_search_term_plan(analysis)

        self.assertEqual("direct", analysis["mode"])
        self.assertEqual("以", analysis["display_term"])
        self.assertEqual([], analysis["concept_terms"])
        self.assertEqual([{"term": "以", "basis": "single_char"}], plan)


class DictionaryContractTests(unittest.TestCase):
    def test_moe_idiom_query_uses_dedicated_endpoint_contract(self):
        expected_entry = {"headword": "畫蛇添足", "match_mode": "exact_headword"}
        with mock.patch.object(
            backend,
            "_search_moe_idioms_entries",
            return_value={
                "available": True,
                "source_id": "moe_idioms",
                "source_mode": "sqlite_exact",
                "entries": [expected_entry],
            },
        ):
            payload = backend.dict_moe_idioms("画蛇添足", 3)

        self.assertTrue(payload["available"])
        self.assertEqual("moe_idioms", payload["source_id"])
        self.assertEqual("sqlite_exact", payload["source_mode"])
        self.assertEqual([expected_entry], payload["entries"])

    def test_page_image_dictionary_does_not_claim_idiom_availability(self):
        with (
            mock.patch.object(backend, "_load_headword_page_candidates", return_value=[]),
            mock.patch.object(backend, "_build_dict_db_entries", return_value=[]),
        ):
            payload = backend.dict_search("画蛇添足", 3)

        self.assertFalse(payload["available"])
        self.assertEqual("page_images", payload["display_mode"])
        self.assertEqual("unavailable", payload["source_mode"])

    def test_maintenance_smoke_targets_the_idiom_endpoint(self):
        manual = (REPO_ROOT / "docs" / "MAINTENANCE_MANUAL.md").read_text(encoding="utf-8")
        self.assertIn("/api/dict/moe-idioms?q=画蛇添足", manual)
        self.assertNotIn("/api/dict/search?q=画蛇添足", manual)


class DependencyContractTests(unittest.TestCase):
    def test_transformers_pin_stays_at_the_verified_security_floor(self):
        requirements = (REPO_ROOT / "requirements.runtime.txt").read_text(encoding="utf-8").splitlines()
        self.assertIn("transformers==5.5.0", requirements)
        self.assertNotIn("transformers==5.2.0", requirements)


@unittest.skipUnless(
    os.environ.get("SUN_TEST_TEXTBOOK_DB") and os.environ.get("SUN_TEST_IDIOM_DB"),
    "authoritative read-only runtime assets were not supplied",
)
class RuntimeAssetSmokeTests(unittest.TestCase):
    @staticmethod
    def _open_read_only(path: Path) -> sqlite3.Connection:
        con = sqlite3.connect(
            f"file:{path}?mode=ro&immutable=1",
            uri=True,
            check_same_thread=False,
        )
        con.row_factory = sqlite3.Row
        return con

    def test_fixed_smokes_against_authoritative_assets_without_writes(self):
        textbook_db = Path(os.environ["SUN_TEST_TEXTBOOK_DB"]).resolve()
        idiom_db = Path(os.environ["SUN_TEST_IDIOM_DB"]).resolve()
        self.assertTrue(textbook_db.is_file())
        self.assertTrue(idiom_db.is_file())

        def open_textbook_db():
            return self._open_read_only(textbook_db)

        with (
            mock.patch.object(backend, "DB_PATH", textbook_db),
            mock.patch.object(backend, "get_db", side_effect=open_textbook_db),
            mock.patch.object(backend, "log_search", return_value=None),
        ):
            backend._get_concept_catalog.cache_clear()
            search_payload = backend.search(
                q="以",
                subject="语文",
                scope_subject=None,
                book_key=None,
                source="textbook",
                phase=None,
                sort="relevance",
                has_images=False,
                limit=3,
                offset=0,
            )

        class _TestConverter:
            @staticmethod
            def convert(text: str) -> str:
                return "畫蛇添足" if text == "画蛇添足" else text

        def open_idiom_db():
            return self._open_read_only(idiom_db)

        with (
            mock.patch.object(backend, "DICT_MOE_IDIOMS_DB_PATH", idiom_db),
            mock.patch.object(backend, "_get_moe_idioms_db", side_effect=open_idiom_db),
            mock.patch.object(backend, "_S2T_CONVERTER", _TestConverter()),
        ):
            idiom_payload = backend.dict_moe_idioms("画蛇添足", 3)

        self.assertGreater(search_payload["total"], 0)
        self.assertEqual("direct", search_payload["query_analysis"]["mode"])
        self.assertEqual("以", search_payload["query_analysis"]["display_term"])
        self.assertEqual(
            [{"term": "以", "basis": "single_char"}],
            search_payload["query_analysis"]["search_plan"],
        )
        self.assertTrue(idiom_payload["available"])
        self.assertEqual("sqlite_exact", idiom_payload["source_mode"])
        self.assertGreater(len(idiom_payload["entries"]), 0)


if __name__ == "__main__":
    unittest.main()
