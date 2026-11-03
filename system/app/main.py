"""
Entry point aplikasi FastAPI — IT Helpdesk Assistant.

Alur endpoint utama:
  1. Menerima pertanyaan/permintaan dari user
  2. Meneruskan ke router (router.py) untuk menentukan intent
  3. Router memanggil RAG (rag.py) atau tool (tools.py) sesuai intent
  4. Mengembalikan response terstruktur ke client
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv

# Muat .env (untuk run lokal) SEBELUM import modul yang membaca environment variables.
# Di Docker, variabel dikirim via `--env-file .env`, sehingga file .env tidak ikut di image.
load_dotenv()

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from app import rag  # noqa: E402
from app.llm import LLMError  # noqa: E402
from app.observability import start_request_metrics  # noqa: E402
from app.router import handle_query  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("helpdesk")

# Metrik latency/token hanya diekspos di response bila diaktifkan (untuk evaluasi),
# agar detail internal tidak bocor ke client di lingkungan produksi.
EXPOSE_METRICS = os.environ.get("EVAL_EXPOSE_METRICS", "false").lower() == "true"


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Bangun index FAISS sekali saat startup (bukan di setiap request)."""
    try:
        await asyncio.to_thread(rag.build_index)
    except Exception:
        # Jangan gagalkan startup; rag.semantic_search akan mencoba lazy-build lagi.
        logger.exception("Gagal membangun FAISS index saat startup, akan dicoba ulang saat request")
    yield


app = FastAPI(
    title="IT Helpdesk Assistant API",
    description="RAG + router/tool-call untuk pertanyaan People & IT Operations.",
    lifespan=lifespan,
)


class AskRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000, examples=["Berapa jatah cuti tahunan?"])


class AskResponse(BaseModel):
    status: str
    intent: str
    data: str
    meta: Optional[dict] = None


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError):
    """Request tidak valid (misal query kosong) tetap dibalas dengan format terstruktur."""
    return JSONResponse(
        status_code=422,
        content={
            "status": "error",
            "intent": "invalid_request",
            "data": "Request tidak valid: field 'query' wajib diisi (1-1000 karakter).",
        },
    )


@app.get("/health")
async def health():
    """Endpoint sederhana untuk memastikan service berjalan."""
    return {"status": "ok", "index_ready": rag._index is not None}


def _finalize(payload: dict, metrics: dict, started: float, status_code: int = 200):
    """Lengkapi metrik total latency, log ringkas, dan (opsional) sisipkan ke response."""
    metrics["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    logger.info(
        "metrics intent=%s total_ms=%s stages=%s tokens=%s",
        payload.get("intent"), metrics["total_ms"], metrics["stages_ms"], metrics["tokens"],
    )
    if EXPOSE_METRICS:
        payload = {**payload, "meta": metrics}
    if status_code == 200:
        return AskResponse(**payload)
    return JSONResponse(status_code=status_code, content=payload)


@app.post("/ask", response_model=AskResponse, response_model_exclude_none=True)
async def ask(request: AskRequest):
    """
    Endpoint utama. Menerima pertanyaan bebas dari user (bisa berupa
    pertanyaan informasi/kebijakan ATAU permintaan cek status tiket),
    lalu meneruskannya ke router untuk diproses.

    Penanganan error:
      - LLM gagal merespons (timeout, quota, key invalid) -> 503 terstruktur
      - Intent tidak dikenali -> 200 dengan status "error", intent "unknown" (dari router)
      - Error tak terduga -> 500 terstruktur (detail hanya di log server)
    """
    started = time.perf_counter()
    metrics = start_request_metrics()

    query = request.query.strip()
    if not query:
        return _finalize(
            {"status": "error", "intent": "invalid_request", "data": "Query tidak boleh kosong."},
            metrics, started, status_code=422,
        )

    try:
        result = await handle_query(query)
        return _finalize(result, metrics, started)
    except LLMError as exc:
        logger.error("LLM error untuk query %r: %s", query, exc)
        return _finalize(
            {
                "status": "error",
                "intent": "llm_unavailable",
                "data": "Maaf, layanan AI sedang tidak dapat merespons. Silakan coba beberapa saat lagi.",
            },
            metrics, started, status_code=503,
        )
    except Exception:
        logger.exception("Unexpected error untuk query %r", query)
        return _finalize(
            {
                "status": "error",
                "intent": "internal_error",
                "data": "Terjadi kesalahan internal pada server. Silakan coba lagi.",
            },
            metrics, started, status_code=500,
        )
