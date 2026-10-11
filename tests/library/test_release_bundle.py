"""The release tooling must ship everything the application imports and serves.

Derived mechanically, not from a hand-kept list:
  * the exact release file list (scripts/stage_clean_release.py) is staged into a
    temporary directory;
  * backend `main` is imported there in a subprocess with a minimal environment;
  * every local asset referenced by the four HTML pages, by the staged CSS and by
    the staged scripts (including the vendored PDF.js paths library.js builds) must
    exist in the staged tree;
  * the pre-cutover image check of scripts/deploy_vps.sh is executed against the
    staged tree (and must fail when a module is missing).

Nothing here contacts a host, runs docker, or runs deploy_vps.sh itself.
Run:  python -m unittest discover -s tests/library
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import build_release_manifest  # noqa: E402
import stage_clean_release  # noqa: E402

SUBPROCESS_TIMEOUT = 120
PAGES = ("index.html", "dict.html", "chuzhong.html", "chuzhong-dict.html")


def stage(target: Path) -> list[str]:
    files = stage_clean_release.release_files()
    for relative in files:
        stage_clean_release.stage_file(relative, target)
    return files


class ReleaseBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sun-release-bundle-"))
        cls.stage = cls.tmp / "stage"
        cls.stage.mkdir()
        cls.files = stage(cls.stage)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def minimal_env(self, stage_dir: Path) -> dict:
        data = self.tmp / "data"
        (data / "index").mkdir(parents=True, exist_ok=True)
        return {
            "PATH": os.environ.get("PATH", ""), "HOME": str(self.tmp), "LANG": "C.UTF-8",
            "PYTHONPATH": str(stage_dir / "backend"), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
            "PROJECT_ROOT": str(stage_dir), "DATA_ROOT": str(data), "STATE_ROOT": str(self.tmp / "state"),
            "RUNTIME_DB_SYNC_MODE": "disabled", "RERANKER_ENABLED": "0", "RERANKER_PRELOAD": "0",
            "SUPPLEMENTAL_VECTOR_ENABLED": "0", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        }

    # (a) the staged tree alone can start the application
    def test_staged_backend_imports_main(self):
        try:
            import fastapi  # noqa: F401
        except ImportError:
            self.skipTest("fastapi is not installed in this interpreter")
        code = (
            "import sys, pathlib\n"
            "import main, ai_guard, library, page_images\n"
            "assert pathlib.Path(page_images.WORKER).is_file(), page_images.WORKER\n"
            "stage = pathlib.Path(sys.argv[1]).resolve()\n"
            "for module in (main, ai_guard, library, page_images):\n"
            "    assert pathlib.Path(module.__file__).resolve().is_relative_to(stage), module.__file__\n"
            "paths = {getattr(r, 'path', None) for r in main.app.routes}\n"
            "need = {'/', '/chuzhong.html', '/dict.html', '/chuzhong-dict.html', '/api/health', '/api/search',\n"
            "        '/files/page/{edition_id}/{sha12}/{page_name}', '/files/cover/{name}', '/files/legacy-page/{jc}/{name}',\n"
            "        '/api/library/legacy-page', '/api/page-image',\n"
            "        '/api/library/release', '/api/library/downloads', '/files/pdf/{edition_id}/{sha12}/{filename}',\n"
            "        '/assets', '/vendor'}\n"
            "assert need <= paths, sorted(need - paths)\n"
            "print('IMPORT_OK', len(paths))\n"
        )
        result = subprocess.run([sys.executable, "-c", code, str(self.stage)], cwd=self.stage, env=self.minimal_env(self.stage),
                                capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        self.assertIn("IMPORT_OK", result.stdout)

    # (b) everything the pages and scripts reference is in the staged tree
    def local_refs(self, text: str) -> set[str]:
        refs = set()
        for value in re.findall(r"""(?:href|src)\s*=\s*["']([^"'#]+)["']""", text):
            if re.match(r"^(?:[a-z]+:)?//|^[a-z]+:", value) or value.startswith("?") \
                    or value.startswith("/") and not value.startswith(("/assets/", "/vendor/")):
                continue   # external, in-page (?view=…) or another page
            refs.add(value.split("?", 1)[0].lstrip("/"))
        return refs

    def test_every_asset_referenced_by_the_pages_is_staged(self):
        checked = 0
        for page in PAGES:
            text = (self.stage / "frontend" / page).read_text(encoding="utf-8")
            refs = self.local_refs(text)
            self.assertTrue(refs, page)
            for ref in refs:
                self.assertTrue((self.stage / "frontend" / ref).is_file(), f"{page} references {ref}, which is not in the release")
                checked += 1
        self.assertGreaterEqual(checked, 8)
        for required in ("assets/library.js", "assets/library.css", "assets/ai-client.js"):
            self.assertIn(required, self.local_refs("".join((self.stage / "frontend" / p).read_text(encoding="utf-8") for p in PAGES)))

    def test_css_and_script_references_are_staged(self):
        assets = self.stage / "frontend" / "assets"
        for css in assets.glob("*.css"):
            for value in re.findall(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", css.read_text(encoding="utf-8")):
                if re.match(r"^(?:data:|https?:|//|#)", value):
                    continue
                self.assertTrue((css.parent / value.split("?", 1)[0]).is_file(), f"{css.name} references {value}")
        for script in assets.glob("*.js"):
            text = script.read_text(encoding="utf-8")
            for value in re.findall(r"""['"`](/(?:assets|vendor)/[A-Za-z0-9_./-]+)['"`?]""", text):
                target = self.stage / "frontend" / value.lstrip("/")
                self.assertTrue(target.exists(), f"{script.name} references {value}")

    def test_vendor_paths_built_by_library_js_are_staged(self):
        text = (self.stage / "frontend" / "assets" / "library.js").read_text(encoding="utf-8")
        base = re.search(r"const PDFJS_BASE = '([^']+)'", text)
        self.assertIsNotNone(base)
        self.assertEqual(base.group(1), "/vendor/pdfjs/")
        pieces = re.findall(r"PDFJS_BASE \+ '([^']+)'", text)
        self.assertGreaterEqual(len(pieces), 4, pieces)
        for piece in pieces:
            target = self.stage / "frontend" / "vendor" / "pdfjs" / piece.split("?", 1)[0]
            if piece.endswith("/"):
                self.assertTrue(target.is_dir() and any(target.iterdir()), piece)
            else:
                self.assertTrue(target.is_file(), piece)
        version = re.search(r"const PDFJS_VERSION = '([^']+)'", text).group(1)
        vendor = json.loads((self.stage / "frontend" / "vendor" / "pdfjs" / "VENDOR.json").read_text(encoding="utf-8"))
        self.assertEqual(vendor["version"], version)
        stage_clean_release.verify_vendor_tree(self.stage)

    def test_vendor_check_catches_a_changed_or_extra_file(self):
        copy = self.tmp / "tampered"
        shutil.copytree(self.stage / "frontend" / "vendor", copy / "frontend" / "vendor")
        (copy / "frontend" / "vendor" / "pdfjs" / "extra.js").write_text("x")
        with self.assertRaises(RuntimeError):
            stage_clean_release.verify_vendor_tree(copy)
        (copy / "frontend" / "vendor" / "pdfjs" / "extra.js").unlink()
        target = copy / "frontend" / "vendor" / "pdfjs" / "legacy" / "build" / "pdf.min.mjs"
        target.write_bytes(target.read_bytes() + b"\n")
        with self.assertRaises(RuntimeError):
            stage_clean_release.verify_vendor_tree(copy)

    # the two lists and the tracked tree agree
    def test_release_list_covers_everything_tracked_under_backend_and_frontend(self):
        try:
            tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "backend", "frontend"], capture_output=True,
                                     text=True, timeout=30, check=True).stdout.split("\n")
        except (OSError, subprocess.SubprocessError):
            self.skipTest("git is not available")
        tracked = {line for line in tracked if line and (ROOT / line).is_file()}
        self.assertTrue(tracked)
        self.assertEqual(tracked - set(self.files), set(), "tracked runtime files missing from RELEASE_FILES / RELEASE_TREES")

    def test_manifest_builder_lists_every_runtime_file(self):
        manifest_paths = set(build_release_manifest.source_asset_paths())
        runtime = {f for f in self.files if f.startswith(("backend/", "frontend/"))}
        self.assertEqual(runtime - manifest_paths, set(), "release files missing from build_release_manifest.py")
        self.assertEqual(len(manifest_paths), len(build_release_manifest.source_asset_paths()))

    def test_committed_manifest_lists_every_runtime_file(self):
        manifest = json.loads((ROOT / "release_manifest.json").read_text(encoding="utf-8"))
        listed = {entry["logical_path"] for entry in manifest["source_assets"]}
        runtime = {f for f in self.files if f.startswith(("backend/", "frontend/"))}
        missing = sorted(runtime - listed)
        self.assertFalse(missing, f"release_manifest.json is stale ({len(missing)} runtime files not listed, e.g. {missing[:3]}): "
                                  "run scripts/build_release_manifest.py "
                                  "(--reuse-runtime-from release_manifest.json for a code-only release)")
        self.assertEqual(len(manifest["runtime_assets"]), len(build_release_manifest.RUNTIME_ASSETS))

    def test_no_bytecode_or_secrets_shaped_files_are_staged(self):
        for relative in self.files:
            self.assertFalse(relative.endswith((".pyc", ".env", ".pem", ".key")), relative)
            self.assertNotIn("__pycache__", relative)


class PageHookTests(unittest.TestCase):
    """The few lines the library tabs own in the existing pages."""

    def test_changed_assets_share_one_new_cache_key(self):
        for page in ("index.html", "chuzhong.html"):
            html = (ROOT / "frontend" / page).read_text(encoding="utf-8")
            keys = {name: re.search(re.escape(name) + r"\?v=([0-9a-z]+)", html).group(1)
                    for name in ("assets/app.js", "assets/library.js", "assets/library.css")}
            self.assertEqual(len(set(keys.values())), 1, keys)
            self.assertNotIn(keys["assets/app.js"], ("20260507a", "20261010a"), page)
        version = json.loads((ROOT / "frontend" / "assets" / "version.json").read_text(encoding="utf-8"))
        app_js = (ROOT / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn(f"const DEFAULT_FRONTEND_VERSION = '{version['frontend_refactor_version']}';", app_js)
        self.assertEqual(version["history"][0]["version"], version["frontend_refactor_version"])

    def test_no_page_sends_readers_to_the_standalone_download_site(self):
        for page in PAGES:
            html = (ROOT / "frontend" / page).read_text(encoding="utf-8")
            self.assertNotIn("jks.bdfz.net", html, page)
        index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="?view=downloads" data-lib-view="downloads"', index)


class DeployScriptStaticTests(unittest.TestCase):
    """Text-level checks of scripts/deploy_vps.sh. The script itself is never run."""

    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / "scripts" / "deploy_vps.sh").read_text(encoding="utf-8")

    def test_shell_syntax(self):
        result = subprocess.run(["bash", "-n", str(ROOT / "scripts" / "deploy_vps.sh")], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def run_blocks(self) -> list[str]:
        blocks = re.findall(r"docker run -d \\\n(?:.*\\\n)+.*\n", self.text)
        self.assertEqual(len(blocks), 2, "expected the start block and the rollback block")
        return blocks

    def test_requirements_pin_the_render_libraries(self):
        text = (ROOT / "requirements.runtime.txt").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^pymupdf==1\.28\.2$")
        self.assertRegex(text, r"(?m)^pillow==\d+\.\d+\.\d+$")

    def test_page_cache_is_the_only_read_write_part_of_the_textbook_root(self):
        self.assertIn('SUN_PAGE_CACHE_DIR="${SUN_PAGE_CACHE_DIR:-${SUN_TEXTBOOK_ROOT}/page-cache}"', self.text)
        self.assertIn('-v "${SUN_TEXTBOOK_ROOT}:${SUN_TEXTBOOK_ROOT}:ro"', self.text)
        self.assertIn('-v "${SUN_PAGE_CACHE_DIR}:${SUN_PAGE_CACHE_DIR}:rw"', self.text)
        self.assertLess(self.text.index(':${SUN_TEXTBOOK_ROOT}:ro"'), self.text.index(':${SUN_PAGE_CACHE_DIR}:rw"'))
        self.assertIn('install -d -m 0755 -o root -g root "${SUN_PAGE_CACHE_DIR}"', self.text)
        self.assertEqual(len(re.findall(r":rw\"", self.text)), 1)

    def test_both_run_blocks_publish_on_loopback_and_take_the_env_file(self):
        for block in self.run_blocks():
            self.assertIn('-p "${PUBLISH_ADDR}:8080:8080"', block)
            self.assertIn('${runtime_env_args[@]+"${runtime_env_args[@]}"}', block)
            self.assertIn('${runtime_mount_args[@]+"${runtime_mount_args[@]}"}', block)
        self.assertNotRegex(self.text, r"-p\s+8080:8080")
        self.assertNotRegex(self.text, r"-p\s+0\.0\.0\.0")
        self.assertIn('PUBLISH_ADDR="${PUBLISH_ADDR:-127.0.0.1}"', self.text)

    def test_env_file_is_optional_outside_the_repo_and_never_printed(self):
        self.assertIn('SUN_ENV_FILE="${SUN_ENV_FILE:-/etc/sun/sun.env}"', self.text)
        self.assertIn('runtime_env_args=()', self.text)
        self.assertIn('runtime_env_args=(--env-file "${SUN_ENV_FILE}")', self.text)
        for forbidden in (r"cat\s+[\"']?\$\{?SUN_ENV_FILE", r"source\s+[\"']?\$\{?SUN_ENV_FILE", r"\.\s+[\"']?\$\{?SUN_ENV_FILE",
                          r"docker inspect[^\n]*Env", r"\bset -x\b", r"\benv\s*$", r"printenv"):
            self.assertNotRegex(self.text, forbidden)
        # no AI_/LIBRARY_ value is written in the script itself
        self.assertNotRegex(self.text, r"-e\s+(?:AI_|LIBRARY_)[A-Z_]+=")

    def test_image_is_checked_before_the_running_container_is_stopped(self):
        check = self.text.index("if ! verify_built_image_release_assets; then")
        stop = self.text.index('docker stop "${CONTAINER_NAME}"')
        build = self.text.index('docker build --pull')
        self.assertLess(build, check)
        self.assertLess(check, stop)
        body = self.text[self.text.index("verify_built_image_release_assets() {"):self.text.index("verify_release_manifest_alignment() {")]
        self.assertIn("import ai_guard, library, page_images", body)
        self.assertIn("import pymupdf", body)
        self.assertIn('features.check("webp")', body)
        self.assertIn("/app/backend/page_render_worker.py", body)
        for name in ("assets/library.js", "assets/library.css", "assets/ai-client.js", "vendor/pdfjs/VENDOR.json",
                     "vendor/pdfjs/legacy/build/pdf.min.mjs", "vendor/pdfjs/legacy/build/pdf.worker.min.mjs"):
            self.assertIn(name, body)

    def image_check_script(self, app_root: Path) -> str:
        body = self.text[self.text.index("verify_built_image_release_assets() {"):self.text.index("verify_release_manifest_alignment() {")]
        match = re.search(r"-lc '\n(.*)\n  '\n\}", body, re.S)
        self.assertIsNotNone(match, "could not extract the in-image check")
        script = match.group(1)
        self.assertNotIn("'", script)   # it is passed inside single quotes
        return script.replace("/app/", str(app_root) + "/").replace("python - <<", f"{sys.executable} - <<")

    def test_image_check_passes_on_the_staged_tree_and_fails_without_a_module(self):
        try:
            import fastapi  # noqa: F401
        except ImportError:
            self.skipTest("fastapi is not installed in this interpreter")
        tmp = Path(tempfile.mkdtemp(prefix="sun-image-check-"))
        try:
            good = tmp / "good"
            good.mkdir()
            stage(good)
            env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(good / "backend"), "PYTHONDONTWRITEBYTECODE": "1",
                   "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"}
            result = subprocess.run(["sh", "-c", self.image_check_script(good)], capture_output=True, text=True, env=env,
                                    timeout=SUBPROCESS_TIMEOUT)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            self.assertIn("image check ok", result.stdout)

            for victim in ("backend/library.py", "backend/ai_guard.py", "backend/page_images.py",
                           "backend/page_render_worker.py", "frontend/assets/library.js",
                           "frontend/vendor/pdfjs/cmaps/UniGB-UTF16-H.bcmap"):
                bad = tmp / "bad"
                shutil.rmtree(bad, ignore_errors=True)
                shutil.copytree(good, bad)
                (bad / victim).unlink()
                env["PYTHONPATH"] = str(bad / "backend")
                result = subprocess.run(["sh", "-c", self.image_check_script(bad)], capture_output=True, text=True, env=env,
                                        timeout=SUBPROCESS_TIMEOUT)
                self.assertNotEqual(result.returncode, 0, victim)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_warm_up_query_is_not_logged_as_a_search(self):
        match = re.search(r"--data-urlencode 'q=([^']+)'", self.text)
        self.assertIsNotNone(match)
        main_text = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
        pattern = re.search(r'TRENDING_QUERY_EXCLUDE_RE = re\.compile\(\s*r"([^"]+)"', main_text)
        self.assertIsNotNone(pattern)
        self.assertRegex(match.group(1), re.compile(pattern.group(1), re.I))


if __name__ == "__main__":
    unittest.main()
