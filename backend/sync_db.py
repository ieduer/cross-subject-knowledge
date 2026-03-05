import os, urllib.request, hashlib
from pathlib import Path

DB_URL = "https://img.rdfzer.com/db-sync/textbook_mineru_fts.db"
PROJECT_ROOT = Path(os.getenv("PROJECT_ROOT", Path(__file__).resolve().parents[1])).expanduser().resolve()
_DEFAULT_DATA_ROOT = PROJECT_ROOT / "data"
_ALT_DATA_ROOT = PROJECT_ROOT.parent / "data"
if not (_DEFAULT_DATA_ROOT / "index").exists() and (_ALT_DATA_ROOT / "index").exists():
    _DEFAULT_DATA_ROOT = _ALT_DATA_ROOT
DATA_ROOT = Path(os.getenv("DATA_ROOT", _DEFAULT_DATA_ROOT)).expanduser().resolve()
DB_PATH = DATA_ROOT / "index/textbook_mineru_fts.db"

def download_db():
    print(f"Checking for DB updates from {DB_URL}...")
    try:
        req = urllib.request.Request(DB_URL, method='HEAD')
        with urllib.request.urlopen(req, timeout=10) as resp:
            remote_size = int(resp.headers.get('Content-Length', 0))
            
        local_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
        print(f"Local size: {local_size}, Remote size: {remote_size}")
        
        # If sizes differ significantly, or local DB is missing, download it
        if not DB_PATH.exists() or remote_size > 0 and abs(local_size - remote_size) > 1024:
            print("Downloading updated textbook_mineru_fts.db from R2...")
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = DB_PATH.with_suffix('.tmp')
            urllib.request.urlretrieve(DB_URL, str(tmp_path))
            tmp_path.rename(DB_PATH)
            print("Database update complete.")
        else:
            print("Database is up to date.")
            
    except Exception as e:
        print(f"Failed to check/download DB update: {e}")

if __name__ == '__main__':
    download_db()
