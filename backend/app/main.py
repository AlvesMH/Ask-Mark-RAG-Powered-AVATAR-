import os, pathlib
from dotenv import load_dotenv, find_dotenv, dotenv_values
import json
import time
from typing import Any, Dict, Optional, cast

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import jwt

# ---- Load envs
ROOT = pathlib.Path(__file__).resolve().parents[2]   # project root (.. / .. from backend/app/main.py)
ENV_FILE = ROOT / ".env"

loaded = []
if ENV_FILE.exists():
    # Merge keys from project .env without overriding OS env
    os.environ.update({k: v for k, v in dotenv_values(ENV_FILE).items()
                       if k and v is not None and k not in os.environ})
    load_dotenv(str(ENV_FILE), override=False)
    loaded.append(str(ENV_FILE))

# Also load any .env under current working dir (for uvicorn --reload cases)
cwd_env = find_dotenv(usecwd=True)
if cwd_env:
    load_dotenv(cwd_env, override=False)
    loaded.append(cwd_env)

# ---- Pinecone Integrated Models (auto-embeddings)
# Requires: pip install --upgrade "pinecone>=5"
try:
    from pinecone import Pinecone
except Exception as e:
    raise RuntimeError("Pinecone SDK not available. Install with: pip install 'pinecone>=5'") from e

PC_CLOUD = os.getenv("PINECONE_CLOUD", "aws")
PC_REGION = os.getenv("PINECONE_REGION", "us-east-1")
PC_API_KEY = os.getenv("PINECONE_API_KEY")
if not PC_API_KEY:
    raise RuntimeError("Missing PINECONE_API_KEY")

INDEX_DOCS_NAME = os.getenv("PINECONE_INDEX_DOCS", "user-docs-index")
INDEX_CHAT_NAME = os.getenv("PINECONE_INDEX_CHAT", "user-chat-index")

# Hosted embedding model; map Pinecone "text" to our field "chunk_text"
EMBED_MODEL = os.getenv("PINECONE_EMBED_MODEL", "llama-text-embed-v2")
FIELD_MAP = {"text": "chunk_text"}

pc = Pinecone(api_key=PC_API_KEY)

def _wait_until_ready(name: str, timeout: float = 90.0, poll: float = 2.0) -> dict:
    """Poll describe_index until 'host' appears (some SDKs also expose status)."""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        try:
            meta = pc.describe_index(name)
            if isinstance(meta, dict):
                last = meta
            else:
                # object style → normalize to dict-ish
                last = {
                    "name": getattr(meta, "name", None),
                    "host": getattr(meta, "host", None),
                    "status": getattr(getattr(meta, "status", None), "ready", None),
                }
            if last.get("host"):
                return last
        except Exception:
            pass
        time.sleep(poll)
    return last

def _ensure_integrated_index(name: str) -> str:
    """
    Ensure an integrated-model index exists.
    Returns a locator string: preferably the index host (domain), else the index name.
    """
    if not pc.has_index(name):
        embed_cfg: Dict[str, Any] = {
            "model": EMBED_MODEL,
            "field_map": FIELD_MAP,   # {"text": "chunk_text"}
        }
        # Cast 'embed' to Any to satisfy Pylance across SDK versions
        pc.create_index_for_model(
            name=name,
            cloud=PC_CLOUD,
            region=PC_REGION,
            embed=cast(Any, embed_cfg),
        )

    meta = _wait_until_ready(name)
    host = meta.get("host") if isinstance(meta, dict) else None
    return host or name

def _looks_like_host(locator: str) -> bool:
    s = (locator or "").strip().lower()
    return ".pinecone.io" in s or "." in s

DOCS_LOCATOR = _ensure_integrated_index(INDEX_DOCS_NAME)
CHAT_LOCATOR = _ensure_integrated_index(INDEX_CHAT_NAME)

# Prefer host targeting; fall back to name if host isn't available
index_docs = pc.Index(host=DOCS_LOCATOR) if _looks_like_host(DOCS_LOCATOR) else pc.Index(name=DOCS_LOCATOR)
index_chat = pc.Index(host=CHAT_LOCATOR) if _looks_like_host(CHAT_LOCATOR) else pc.Index(name=CHAT_LOCATOR)

# ---- FastAPI app
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # restrict in prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def json_or_text(resp):
    try:
        return resp.json()
    except Exception:
        return {"text": resp.text}

def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    uid = payload.get("sub")
    if not uid:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    return {"id": uid, "email": payload.get("email", "")}

from backend.app.routes import auth, chat  # type: ignore
app.include_router(auth.router, prefix="/api")
app.include_router(chat.router, prefix="/api")

@app.get("/health")
def health():
    return {
        "status": "healthy",
        "pinecone": {
            "cloud": PC_CLOUD,
            "region": PC_REGION,
            "docs": INDEX_DOCS_NAME,
            "chat": INDEX_CHAT_NAME,
            "docs_locator": DOCS_LOCATOR,
            "chat_locator": CHAT_LOCATOR,
        },
    }

# ---- Static: serve frontend/dist IF it exists
def _resolve_frontend_dist() -> str:
    cand = os.path.abspath(os.path.join(os.getcwd(), "frontend", "dist"))
    if os.path.isdir(cand):
        return cand
    cand2 = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist"))
    if os.path.isdir(cand2):
        return cand2
    return ""  # return empty if not found

FRONTEND_DIST = _resolve_frontend_dist()

if FRONTEND_DIST:
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="static")
else:
    @app.get("/")
    def root():
        return {
            "message": "API OK (frontend not built). Run `npm ci && npm run build` in /frontend to serve the UI."
        }

