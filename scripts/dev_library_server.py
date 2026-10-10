#!/usr/bin/env python3
"""Local dev server for the two library tabs (PDF 预览 / 教材下载).

Runs backend/library.py for real, serves frontend/ statically and forwards the
OTHER `/api/*` GET requests read-only to the production SUN site, so the
existing UI works without the 5 GB of models.  Development only: never deploy.

    python scripts/dev_library_server.py --synthetic
    python scripts/dev_library_server.py --release-dir <…/releases/<id>> --pdf-dir <dir with <sha256>.pdf>

Notes
  * Only GET is forwarded.  POST (chat, logs) is answered locally with 405, so
    this server can never trigger an AI call.
  * Forwarded `/api/search` calls are counted and refused after --max-search
    (default 6): every real search writes a row to production search_logs.
  * The release directory is never modified: a throw-away catalog root with a
    symlink and a CURRENT pointer is created under --work-dir.
  * Needs fastapi, uvicorn, httpx (already runtime deps); --synthetic also
    needs PyMuPDF.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import httpx  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import library  # noqa: E402

FRONTEND = ROOT / "frontend"
PAGES = {"/": "index.html", "/chuzhong.html": "chuzhong.html", "/dict.html": "dict.html",
         "/chuzhong-dict.html": "chuzhong-dict.html"}


def build_app(catalog_root: Path, pdf_dir: Path, upstream: str | None, max_search: int, extra_env: dict) -> FastAPI:
    app = FastAPI(title="SUN library tabs · dev")
    env = {"PDF_SERVE_MODE": "direct", "LIBRARY_PDF_ROOT": str(pdf_dir), "LIBRARY_CATALOG_ROOT": str(catalog_root),
           "LIBRARY_RELOAD_SECONDS": "5"}
    env.update(extra_env)
    library.install(app, data_root=catalog_root.parent, frontend_dir=FRONTEND, env=env)
    app.mount("/assets", StaticFiles(directory=FRONTEND / "assets"), name="assets")

    for route, filename in PAGES.items():
        def page(filename=filename):
            return FileResponse(FRONTEND / filename, media_type="text/html", headers={"Cache-Control": "no-store"})
        app.add_api_route(route, page, methods=["GET"], include_in_schema=False)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    state = {"search": 0}

    @app.get("/api/{path:path}", include_in_schema=False)
    def proxy(path: str, request: Request):
        if path.startswith("library/") or path == "library":
            return JSONResponse({"detail": "not found"}, status_code=404)
        if upstream is None:
            return JSONResponse({"detail": "dev server started with --no-proxy"}, status_code=503)
        if path == "search":
            if state["search"] >= max_search:
                return JSONResponse({"detail": f"dev search budget used ({max_search})"}, status_code=429,
                                    headers={"Retry-After": "3600"})
            state["search"] += 1
            print(f"[dev] forwarded search {state['search']}/{max_search}", flush=True)
        try:
            upstream_response = httpx.get(f"{upstream}/api/{path}", params=list(request.query_params.multi_items()),
                                          timeout=45.0, follow_redirects=False,
                                          headers={"Accept": "application/json", "User-Agent": "sun-library-dev/1"})
        except httpx.HTTPError as exc:
            return JSONResponse({"detail": f"upstream unreachable: {type(exc).__name__}"}, status_code=502)
        return Response(content=upstream_response.content, status_code=upstream_response.status_code,
                        media_type=upstream_response.headers.get("content-type", "application/json"),
                        headers={"Cache-Control": "no-store"})

    @app.api_route("/api/{path:path}", methods=["POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    def no_writes(path: str):
        return JSONResponse({"detail": "dev server is read-only; AI/chat endpoints are not forwarded"}, status_code=405)

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release-dir", type=Path, help="one catalog release directory (…/releases/<id>)")
    parser.add_argument("--pdf-dir", type=Path, help="directory holding <sha256>.pdf files")
    parser.add_argument("--synthetic", action="store_true", help="generate a synthetic release + tiny PDFs")
    parser.add_argument("--work-dir", type=Path, help="scratch directory (default: a new temp dir, removed on exit)")
    parser.add_argument("--upstream", default="https://sun.bdfz.net", help="read-only source for other /api GETs")
    parser.add_argument("--no-proxy", action="store_true")
    parser.add_argument("--max-search", type=int, default=6)
    parser.add_argument("--serve-mode", choices=["direct", "accel"], default="direct")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not args.synthetic and not (args.release_dir and args.pdf_dir):
        parser.error("give --synthetic, or both --release-dir and --pdf-dir")

    owns_work = args.work_dir is None
    work = Path(tempfile.mkdtemp(prefix="sun-library-dev-")) if owns_work else args.work_dir
    work.mkdir(parents=True, exist_ok=True)
    catalog_root = work / "catalog"
    try:
        if args.synthetic:
            sys.path.insert(0, str(ROOT / "tests" / "library"))
            import synthetic
            shutil.rmtree(catalog_root, ignore_errors=True)
            pdf_dir = work / "pdf"
            info = synthetic.build(catalog_root, pdf_dir, big=True)
            print(f"[dev] synthetic release {info['release_id']} in {catalog_root}", flush=True)
        else:
            release_dir = args.release_dir.resolve()
            pdf_dir = args.pdf_dir.resolve()
            if not (release_dir / "catalog.json").is_file():
                parser.error(f"{release_dir} does not look like a release directory")
            shutil.rmtree(catalog_root, ignore_errors=True)
            (catalog_root / "releases").mkdir(parents=True)
            (catalog_root / "releases" / release_dir.name).symlink_to(release_dir, target_is_directory=True)
            (catalog_root / "CURRENT").write_text(release_dir.name + "\n", encoding="utf-8")
        # A generator release records storage.vps only once files are placed on the VPS;
        # in dev, treat the files found in --pdf-dir as stored.
        app = build_app(catalog_root, pdf_dir, None if args.no_proxy else args.upstream.rstrip("/"), args.max_search,
                        {"PDF_SERVE_MODE": args.serve_mode, "LIBRARY_PREVIEW_POLICY": "present"})
        import uvicorn
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        if owns_work:
            shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
