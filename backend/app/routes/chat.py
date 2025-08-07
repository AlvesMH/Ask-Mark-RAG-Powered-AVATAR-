# backend/app/routes/chat.py
import io
import os
import re
import json
import uuid
from typing import Any, Dict, List, Optional, TypedDict, cast

import requests
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from PyPDF2 import PdfReader

# Optional docx support
try:
    from docx import Document as DocxDocument
except Exception:
    DocxDocument = None  # we'll error if used

# --------------------- config / simple metadata store ---------------------

DOCS_META_FILE = os.getenv("DOCS_META_FILE", "docs_meta.json")


def _load_meta() -> Dict[str, List[Dict[str, Any]]]:
    """
    Returns: { user_id: [ {name, type, pages, uploaded_at}, ... ] }
    """
    if not os.path.exists(DOCS_META_FILE):
        return {}
    try:
        with open(DOCS_META_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_meta(meta: Dict[str, List[Dict[str, Any]]]) -> None:
    tmp = f"{DOCS_META_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    os.replace(tmp, DOCS_META_FILE)


# ------------------------------ models -----------------------------------

class ChatRequest(BaseModel):
    message: str
    selected_sources: Optional[List[str]] = None
    temperature: Optional[float] = 0.6


class DocRemoveRequest(BaseModel):
    names: List[str]


# ---- local typedefs so Pylance is happy with search(query=...) ----
class _InputsTD(TypedDict):
    text: str


class _SearchQueryTD(TypedDict):
    inputs: _InputsTD
    top_k: int


# ------------------------------ utils ------------------------------------

def _sanitize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _chunk_text(text: str, max_chars: int = 1200, overlap: int = 200) -> List[str]:
    s = _sanitize(text)
    if not s:
        return []
    out: List[str] = []
    i, n = 0, len(s)
    while i < n:
        j = min(i + max_chars, n)
        if j < n:
            pivot = s.rfind(". ", i, j)
            if pivot == -1:
                pivot = s.rfind(" ", i, j)
            if pivot != -1 and pivot > i + 200:
                j = pivot + 1
        out.append(s[i:j])
        i = max(j - overlap, j)
    return out


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _extract_hits(sres: Any) -> List[Any]:
    result = _get(sres, "result", None)
    if result is None:
        hits = _get(sres, "hits", [])
        return list(hits or [])
    hits = _get(result, "hits", [])
    return list(hits or [])


def _fields_from_hit(hit: Any) -> Dict[str, Any]:
    fields_obj = _get(hit, "fields", None)
    if isinstance(fields_obj, dict):
        return fields_obj
    to_dict = getattr(fields_obj, "to_dict", None)
    if callable(to_dict):
        try:
            d = to_dict()
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    try:
        return dict(fields_obj) if fields_obj is not None else {}
    except Exception:
        return {}


def _voice_clean(s: str) -> str:
    """Remove markdown artifacts and ensure sentence-friendly TTS output."""
    if not s:
        return ""
    s = re.sub(r"```.*?```", "", s, flags=re.S)
    s = re.sub(r"^(\s*[-*•]\s+)", "", s, flags=re.M)
    s = re.sub(r"^#{1,6}\s*", "", s, flags=re.M)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.replace(" -- ", " — ").replace("...", "…")
    if not re.search(r"[.!?…]$", s):
        s += "."
    return s


# ------------------------------ parsing ----------------------------------

def _read_pdf(raw: bytes, name: str) -> List[Dict[str, Any]]:
    """Returns list of {text, page} entries."""
    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read PDF: {name or 'Unknown'}") from e

    items: List[Dict[str, Any]] = []
    for page_num, page in enumerate(reader.pages, start=1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        if not text:
            continue
        for i, chunk in enumerate(_chunk_text(text), start=1):
            items.append({"text": chunk, "page": page_num, "chunk": i})
    return items


def _read_docx(raw: bytes, name: str) -> List[Dict[str, Any]]:
    if DocxDocument is None:
        raise HTTPException(status_code=500, detail="DOCX support missing. Install python-docx.")
    try:
        doc = DocxDocument(io.BytesIO(raw))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read DOCX: {name}") from e
    txt = "\n".join(p.text for p in doc.paragraphs if p.text)
    return [{"text": t, "page": 1, "chunk": i} for i, t in enumerate(_chunk_text(txt), start=1)]


def _read_plain(raw: bytes, name: str) -> List[Dict[str, Any]]:
    try:
        txt = raw.decode("utf-8", errors="ignore")
    except Exception:
        txt = ""
    return [{"text": t, "page": 1, "chunk": i} for i, t in enumerate(_chunk_text(txt), start=1)]


# --------------------------- index helpers --------------------------------

def _clear_namespace(idx: Any, namespace: str) -> None:
    """
    Try a variety of deletion styles so we work with different Pinecone wrappers.
    """
    # Explicit helper names from possible wrappers
    for mname in ("delete_namespace", "clear_namespace", "wipe_namespace", "delete_records"):
        m = getattr(idx, mname, None)
        if callable(m):
            try:
                # Some wrappers are (namespace) and others are (namespace=...)
                try:
                    m(namespace)
                except TypeError:
                    m(namespace=namespace)
                return
            except Exception:
                pass

    # Generic delete(all)
    m = getattr(idx, "delete", None)
    if callable(m):
        # Try various common signatures
        for kwargs in (
            {"namespace": namespace, "delete_all": True},
            {"namespace": namespace, "ids": ["*"]},
        ):
            try:
                m(**kwargs)
                return
            except Exception:
                continue

    # If we get here, raise
    raise HTTPException(status_code=500, detail="Unable to clear namespace.")


def _delete_records_by_source(idx: Any, namespace: str, source_name: str) -> int:
    """
    Delete all records in a namespace where fields.source == source_name.
    Returns count deleted when determinable (0 otherwise).
    """
    # Preferred: records API with filter support
    for mname in ("delete_records",):
        m = getattr(idx, mname, None)
        if callable(m):
            try:
                try:
                    res = m(namespace, filter={"source": source_name})
                except TypeError:
                    res = m(namespace=namespace, filter={"source": source_name})
                # res may be None or dict with 'deleted'
                if isinstance(res, dict) and "deleted" in res:
                    return int(res["deleted"])
                return 0
            except Exception:
                pass

    # Fallback: search for ids then delete
    deleted = 0
    try:
        sres = idx.search(
            namespace=namespace,
            query=cast(Any, {"inputs": {"text": source_name}, "top_k": 5000}),
            fields=["source"],
        )
        ids: List[str] = []
        for hit in _extract_hits(sres):
            fields = _fields_from_hit(hit)
            if fields.get("source") == source_name:
                hid = _get(hit, "id") or _get(hit, "_id")
                if hid:
                    ids.append(str(hid))
        if ids:
            d = getattr(idx, "delete", None)
            if callable(d):
                try:
                    d(namespace=namespace, ids=ids)
                    deleted = len(ids)
                except Exception:
                    pass
    except Exception:
        pass
    return deleted

def _upsert_records_compat(idx: Any, namespace: str, records: List[Dict[str, Any]]):
    """
    Upsert using the integrated-embedding 'records' API when available.
    If the local Pinecone SDK is too old, raise a helpful error.
    """
    m = getattr(idx, "upsert_records", None)
    if callable(m):
        # Some SDKs allow positional (namespace, records); use kwargs for clarity
        return m(namespace=namespace, records=records)
    # Older SDKs don't support integrated-embedding upserts.
    raise HTTPException(
        status_code=500,
        detail=("Your Pinecone client is too old for 'upsert_records'. "
                "Upgrade with: pip install -U 'pinecone>=6'  "
                "Then restart the server.")
    )

# ------------------------------- routes -----------------------------------

@router.post("/upload")
async def upload_files(
    files: List[UploadFile] = File(...),
    current: dict = Depends(get_current_user),
):
    """
    Accept PDF, DOCX, TXT, MD and ingest to Pinecone. We also record doc names in a simple meta file.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded.")

    meta = _load_meta()
    user_docs = {d["name"] for d in meta.get(current["id"], [])}
    files_ingested = 0
    total_pages = 0
    total_chunks = 0

    for f in files:
        name = (f.filename or "").strip()
        low = name.lower()
        if not any(low.endswith(ext) for ext in (".pdf", ".docx", ".txt", ".md")):
            raise HTTPException(status_code=400, detail=f"{name or 'Unknown'} is not a supported type (PDF/DOCX/TXT/MD).")

        raw = await f.read()
        if low.endswith(".pdf"):
            items = _read_pdf(raw, name)
            doc_type = "pdf"
        elif low.endswith(".docx"):
            items = _read_docx(raw, name)
            doc_type = "docx"
        else:  # .txt or .md
            items = _read_plain(raw, name)
            doc_type = "text"

        if not items:
            continue

        records: List[Dict[str, Any]] = []
        for i, it in enumerate(items):
            records.append({
                "_id": f"{current['id']}:doc:{uuid.uuid4().hex}",
                "chunk_text": it["text"],
                "source": name,
                "page": it.get("page", 1),
                "chunk": it.get("chunk", i + 1),
            })
        index_docs.upsert_records(current["id"], records)

        files_ingested += 1
        total_chunks += len(records)
        doc_pages = max(1, len({r["page"] for r in records}))
        total_pages += doc_pages
        
        if name not in user_docs:
            user_docs.add(name)
            meta.setdefault(current["id"], []).append({
                "name": name,
                "type": doc_type,
                "pages": doc_pages,
                "uploaded_at": uuid.uuid1().time,
            })

    _save_meta(meta)

    if files_ingested == 0:
        raise HTTPException(status_code=400, detail="No extractable text found in the uploaded files.")

    return {
        "message": f"Ingested {files_ingested} document(s).",
        "pages": total_pages,
        "chunks": total_chunks,
    }


@router.get("/docs")
def list_docs(current: dict = Depends(get_current_user)):
    """
    Returns the list of doc names previously uploaded by the user.
    """
    meta = _load_meta()
    docs = [d["name"] for d in meta.get(current["id"], [])]
    return {"docs": docs}


@router.delete("/docs")
def remove_docs(req: DocRemoveRequest, current: dict = Depends(get_current_user)):
    """
    Remove one or more documents (by file name) from the user's namespace.
    """
    names = [n.strip() for n in (req.names or []) if n and n.strip()]
    if not names:
        raise HTTPException(status_code=400, detail="No document names supplied.")

    deleted_total = 0
    for nm in names:
        try:
            deleted_total += _delete_records_by_source(index_docs, current["id"], nm)
        except HTTPException:
            raise
        except Exception:
            # continue attempting others
            pass

    # Update meta
    meta = _load_meta()
    if current["id"] in meta:
        meta[current["id"]] = [d for d in meta[current["id"]] if d.get("name") not in names]
        _save_meta(meta)

    return {"removed": names, "deleted": deleted_total}


@router.post("/memory/clear")
def clear_memory(current: dict = Depends(get_current_user)):
    """
    Clear the user's conversation memory namespace.
    """
    try:
        _clear_namespace(index_chat, current["id"])
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Unable to clear memory.")

    return {"cleared": True}


@router.post("/chat")
def chat(req: ChatRequest, current: dict = Depends(get_current_user)):
    query_text = (req.message or "").strip()
    if not query_text:
        raise HTTPException(status_code=400, detail="Empty query.")

    selected = set((req.selected_sources or []))
    temp = float(req.temperature if req.temperature is not None else 0.6)
    temp = max(0.0, min(1.5, temp))

    # 1) Search docs → collect candidate excerpts
    excerpts: List[Dict[str, Any]] = []
    try:
        query_payload: _SearchQueryTD = {"inputs": {"text": query_text}, "top_k": 16}
        sres = index_docs.search(
            namespace=current["id"],
            query=cast(Any, query_payload),
            fields=["chunk_text", "source", "page"],
        )
        for hit in _extract_hits(sres):
            fields = _fields_from_hit(hit)
            txt = fields.get("chunk_text", "") or ""
            src = fields.get("source", "doc") or "doc"
            page = fields.get("page", 1) or 1
            if not txt:
                continue
            if selected and src not in selected:
                continue
            excerpts.append({"source": src, "page": page, "text": _sanitize(txt)})
        excerpts = excerpts[:5]
    except Exception:
        excerpts = []

    # 2) Conversation memory
    conv_ctx: List[str] = []
    try:
        query_payload: _SearchQueryTD = {"inputs": {"text": query_text}, "top_k": 3}
        sres = index_chat.search(
            namespace=current["id"],
            query=cast(Any, query_payload),
            fields=["chunk_text"],
        )
        for hit in _extract_hits(sres):
            fields = _fields_from_hit(hit)
            txt = fields.get("chunk_text", "") or ""
            if txt:
                conv_ctx.append(_sanitize(txt))
    except Exception:
        pass

    # 3) Build a voice-first prompt
    voice_style = (
        "Speak like a warm, lively voice assistant. Use short, conversational sentences, "
        "natural pauses and contractions. Avoid bullet lists, headings, code, emoticons or URLs. "
        "Keep answers to about 3–5 sentences unless asked for more, and include only relevant details. Include all the relevant punctuation for natural speech, but do not read the punctuation symbols. "
        "When useful, refer to documents verbally, e.g., 'the policy on page 3 of HR.pdf'."
    )
    system_prompt = f"You are a helpful assistant.\n{voice_style}"

    if excerpts:
        lines = [f"- [{e['source']} p.{e['page']}] {e['text']}" for e in excerpts]
        ctx_text = "Relevant excerpts:\n" + "\n".join(lines)
        user_prompt = (
            f"{ctx_text}\n\n"
            f"Question: {query_text}\n\n"
            "Using these excerpts as your main evidence, give a single spoken answer that explains or summarizes "
            "what the user needs. Weave in brief, natural attributions (file name and page) instead of bracketed "
            "citations. If something isn't covered and its relevant for the quality of the response, you add general knowledge, but keep it concise."
        )
    else:
        user_prompt = (
            f"Question: {query_text}\n\n"
            "Answer conversationally for voice. Keep it clear and engaging. Include all the relevant punctuation for natural speech, but do not read the punctuation symbols."
        )

    if not SEA_LION_API_KEY:
        raise HTTPException(status_code=500, detail="SEA_LION_API_KEY missing.")

    headers = {"Authorization": f"Bearer {SEA_LION_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": SEA_LION_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temp,
    }

    resp = None
    try:
        resp = requests.post(SEA_LION_URL, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
    except Exception as e:
        snippet = resp.text[:240] if resp is not None else str(e)
        raise HTTPException(status_code=502, detail=f"Sea-Lion error: {snippet}")

    raw = json_or_text(resp)
    data: Dict[str, Any] = raw if isinstance(raw, dict) else {"text": str(raw)}
    answer = ((data.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "") or ""

    # Voice-friendly clean
    answer = _voice_clean(answer)

    # 4) Save conversation to memory (as text)
    convo_text = f"User: {query_text}\nAssistant: {answer}"
    index_chat.upsert_records(
        current["id"],
        [{"_id": f"{current['id']}:chat:{uuid.uuid4().hex}", "chunk_text": convo_text}],
    )

    return {"answer": answer, "excerpts": excerpts}
