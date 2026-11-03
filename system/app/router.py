"""
Router — menentukan apakah query user butuh RAG (informasi/kebijakan)
atau tool call (aksi spesifik, misal cek status tiket).

Pola ini mengikuti workflow router yang sudah dipelajari di Sesi 12.4:
  user input -> intent detection -> jika butuh data -> retrieval (RAG)
                                  -> jika butuh aksi -> tool call

Strategi intent detection (hybrid, 3 lapis):
  1. Rule-based fast path : query mengandung pola ticket_id (TICKET-XXXX)
                            -> pasti 'ticket_status' (deterministik, tanpa LLM).
  2. LLM classifier        : Gemini dengan structured output (enum) untuk
                            membedakan 'faq' / 'ticket_status' / 'unknown'.
  3. Keyword baseline      : dipakai sebagai fallback bila LLM gagal merespons,
                            sehingga layanan tetap berjalan (graceful degradation).
"""

import logging
import re
import time
from typing import Literal, Optional

from google.genai import types
from pydantic import BaseModel

from app.llm import FALLBACK_MODEL, GENERATION_MODEL, LLMError, get_client
from app.observability import get_request_metrics, record_llm_call, record_llm_failure, stage, traceable
from app.rag import answer_from_knowledge_base
from app.tools import get_ticket_status

logger = logging.getLogger(__name__)

VALID_INTENTS = ("faq", "ticket_status", "unknown", "greeting")

TICKET_ID_PATTERN = re.compile(
    r"\b(?:ticket|tiket)\s*(?:no\.?|nomor|nomer|number|nr\.?)?\s*[-_#:]?\s*(\d{1,6})\b",
    re.IGNORECASE,
)

_GREETING_PATTERN = re.compile(
    r"^(?:halo|hai|hi|hello|hei|hey|selamat\s+(?:pagi|siang|sore|malam)|assalamu['a]?laikum)[\s!.,?]*$",
    re.IGNORECASE,
)

_TICKET_KEYWORDS = (
    "status tiket", "status ticket", "cek tiket", "cek ticket", "tiket saya",
    "ticket saya", "progress tiket", "tiketku", "nomor tiket",
)
_FAQ_KEYWORDS = (
    "cuti", "izin", "sakit", "absen", "password", "vpn", "login", "email", "akses",
    "laptop", "perangkat", "software", "install", "resign", "onboarding", "karyawan baru",
    "gaji", "payroll", "rekening", "asuransi", "reimburse", "atasan", "hybrid", "wfh",
    "jam kerja", "lembur", "melahirkan", "paternity", "slack", "google meet",
    "workspace", "pelatihan", "kebijakan", "prosedur", "bagaimana cara",
)


class IntentClassification(BaseModel):
    intent: Literal["faq", "ticket_status", "unknown"]


_INTENT_PROMPT = """Anda adalah intent classifier untuk asisten internal People & IT Operations.
Klasifikasikan pertanyaan karyawan ke tepat SATU kategori:

- "faq": pertanyaan informasi/kebijakan/prosedur umum seputar HR & IT
  (cuti, izin, absensi, password, VPN, akses sistem, perangkat/laptop, software,
  onboarding, gaji/payroll, asuransi, reimbursement, kebijakan kerja, tools kolaborasi).
  Termasuk keluhan/troubleshooting teknis (misal laptop lambat/rusak).
  Jika user menanyakan masalah teknis ATAU query campuran yang memuat pertanyaan informasi teknis sekaligus tiket tanpa ID spesifik, pilih "faq" agar substansi masalahnya dapat dijawab oleh knowledge base.
- "ticket_status": user ingin MENGECEK status/progres tiket helpdesk TERTENTU miliknya.
- "unknown": di luar cakupan HR & IT internal perusahaan (misal cuaca, resep, politik,
  matematika, obrolan umum) atau terlalu ambigu untuk dipahami.

Contoh:
- "Berapa jatah cuti tahunan?" -> faq
- "Gimana cara reset password VPN?" -> faq
- "Kenapa laptop saya lambat dan gimana cara cek tiket yang saya buat soal itu?" -> faq
- "Tolong cek progres tiket TICKET-0003" -> ticket_status
- "Tiket saya sudah sampai mana ya?" -> ticket_status
- "Siapa juara Piala Dunia 2022?" -> unknown

Pertanyaan: {query}"""


def extract_ticket_id(query: str) -> Optional[str]:
    """
    Mengekstrak ticket_id dari teks query dan menormalisasinya ke format
    'TICKET-XXXX' (sesuai mock_tickets.csv). Mengembalikan None bila tidak ada.
    """
    match = TICKET_ID_PATTERN.search(query or "")
    if not match:
        return None
    return f"TICKET-{int(match.group(1)):04d}"


def _keyword_intent(query: str) -> str:
    """Baseline intent detection berbasis keyword (tanpa LLM)."""
    q = query.lower()
    if any(k in q for k in _TICKET_KEYWORDS):
        return "ticket_status"
    if any(k in q for k in _FAQ_KEYWORDS):
        return "faq"
    return "unknown"


def _set_route(layer: str) -> None:
    metrics = get_request_metrics()
    if metrics is not None:
        metrics["intent_layer"] = layer


@traceable(run_type="llm", name="gemini_intent_classifier")
async def _classify_with_model(query: str, model_name: str):
    start = time.perf_counter()
    try:
        response = await get_client().aio.models.generate_content(
            model=model_name,
            contents=_INTENT_PROMPT.format(query=query),
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
                response_schema=IntentClassification,
            ),
        )
    except Exception as exc:
        record_llm_failure("intent_classification", model_name, (time.perf_counter() - start) * 1000, exc)
        raise
    record_llm_call("intent_classification", model_name, response, (time.perf_counter() - start) * 1000)
    return response


async def _llm_intent(query: str) -> str:
    models_to_try = [GENERATION_MODEL]
    if FALLBACK_MODEL and FALLBACK_MODEL != GENERATION_MODEL:
        models_to_try.append(FALLBACK_MODEL)

    for model_name in models_to_try:
        try:
            response = await _classify_with_model(query, model_name)
            parsed = response.parsed
            if isinstance(parsed, IntentClassification):
                return parsed.intent
            return IntentClassification.model_validate_json(response.text).intent
        except Exception as exc:
            logger.warning("Intent classifier dengan %s gagal: %s", model_name, exc)

    raise LLMError("Seluruh model intent classifier gagal merespons.")


@traceable(run_type="chain", name="detect_intent")
async def detect_intent(query: str) -> str:
    """
    Menentukan intent dari query user. Mengembalikan salah satu:
      - "faq"           -> pertanyaan informasi/kebijakan, arahkan ke RAG
      - "ticket_status" -> permintaan cek status tiket, arahkan ke tool call
      - "unknown"       -> di luar cakupan / ambigu
    """
    if _GREETING_PATTERN.match(query.strip()):
        logger.info("Intent via rule (greeting pattern): greeting")
        _set_route("greeting_fastpath")
        return "greeting"

    if extract_ticket_id(query):
        logger.info("Intent via rule (ticket_id pattern): ticket_status")
        _set_route("regex")
        return "ticket_status"

    try:
        with stage("intent_classification"):
            intent = await _llm_intent(query)
        if intent in VALID_INTENTS:
            logger.info("Intent via LLM: %s", intent)
            _set_route("llm")
            return intent
    except Exception:
        logger.exception("Intent detection via LLM gagal, fallback ke keyword baseline")

    intent = _keyword_intent(query)
    logger.info("Intent via keyword baseline: %s", intent)
    _set_route("keyword_fallback")
    return intent


@traceable(run_type="chain", name="handle_query")
async def handle_query(query: str) -> dict:
    """
    Fungsi utama yang dipanggil oleh endpoint /ask.
    Menjalankan intent detection, lalu merutekan ke RAG atau tool call yang sesuai.
    """
    intent = await detect_intent(query)

    if intent == "greeting":
        return {
            "status": "success",
            "intent": "greeting",
            "data": (
                "Halo! Saya IT Helpdesk Assistant. Ada yang bisa saya bantu terkait kebijakan internal "
                "People & IT Operations (seperti cuti, password, perangkat, onboarding) atau pengecekan "
                "status tiket Anda?"
            ),
        }

    elif intent == "ticket_status":
        ticket_id = extract_ticket_id(query)
        with stage("tool_call"):
            data = get_ticket_status(ticket_id)
        return {"status": "success", "intent": "ticket_status", "data": data}

    elif intent == "faq":
        data = await answer_from_knowledge_base(query)
        return {"status": "success", "intent": "faq", "data": data}

    else:
        return {
            "status": "error",
            "intent": "unknown",
            "data": (
                "Maaf, saya tidak memahami permintaan Anda atau permintaan tersebut di luar "
                "cakupan layanan People & IT Operations. Saya dapat membantu menjawab "
                "pertanyaan kebijakan HR/IT (misal cuti, password, perangkat) atau mengecek "
                "status tiket (contoh: 'Apa status tiket TICKET-0005?')."
            ),
        }
