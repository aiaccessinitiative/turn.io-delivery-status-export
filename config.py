"""Central configuration for the Turn.io WhatsApp delivery-export toolkit.

Loads a local `.env` (KEY=VALUE lines) from the repo root and exposes shared
settings + paths. Nothing here is machine- or project-specific: every path is
overridable via an environment variable, and no secrets or PII live in the repo.

Required in .env (see .env.example):
  TURN_TOKEN          Bearer token for the Turn.io Data Export API
  TURN_DISSEM_PATH    Absolute path to your dissemination roster CSV (contains
                      phone numbers + ppbno + geography; kept OUTSIDE the repo)

Optional:
  TURN_BASE_URL       Data API base (default: https://whatsapp.turn.io/v1/data)
  TURN_DATA_DIR       Where outputs are written (default: ./data, gitignored)
  TURN_REFERENCE_DIR  Optional reference files, e.g. canonical geography labels
                      and report templates (default: ./reference)
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# --- load .env (simple KEY=VALUE lines) from the repo root, if present ---
_env = ROOT / ".env"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# --- Turn.io Data Export API ---
TURN_TOKEN = os.environ.get("TURN_TOKEN")
BASE = os.environ.get("TURN_BASE_URL", "https://whatsapp.turn.io/v1/data")
ACCEPT = "application/vnd.v1+json"

# --- paths (all overridable via env; none committed to the repo) ---
DISSEM_PATH = Path(os.environ["TURN_DISSEM_PATH"]) if os.environ.get("TURN_DISSEM_PATH") else None
DATA_DIR = Path(os.environ.get("TURN_DATA_DIR", str(ROOT / "data")))
REFERENCE_DIR = Path(os.environ.get("TURN_REFERENCE_DIR", str(ROOT / "reference")))

DATA_DIR.mkdir(parents=True, exist_ok=True)


def require_token() -> str:
    if not TURN_TOKEN:
        raise SystemExit("ERROR: TURN_TOKEN not set. Copy .env.example to .env and add your token.")
    return TURN_TOKEN


def require_dissem() -> Path:
    if not DISSEM_PATH or not DISSEM_PATH.exists():
        raise SystemExit(
            "ERROR: dissemination roster not found. Set TURN_DISSEM_PATH in .env to your local "
            "CSV with columns: MobileNo, ppbno, district_name, mandal_name, cluster_name, village_name."
        )
    return DISSEM_PATH
