"""
Observability — tracing LangSmith + pengumpulan metrik per-request.

Modul ini ditambahkan pada tahap *Observe* (Assignment 5) dan TIDAK mengubah
perilaku sistem. Tujuannya agar setiap request dapat diukur secara objektif:
  - latency per tahap (intent classifier, embedding/retrieval, LLM generation, tool)
  - jumlah token (prompt / output / total) per panggilan LLM
  - trace end-to-end di LangSmith (bila LANGSMITH_TRACING=true & API key tersedia)

Metrik dikumpulkan via ContextVar sehingga aman untuk request asynchronous
yang berjalan bersamaan.
"""

import contextvars
import time
from contextlib import contextmanager
from typing import Any, Optional

try:  # LangSmith opsional: bila paket/kredensial tidak ada, tracing menjadi no-op.
    from langsmith import traceable
    from langsmith.run_helpers import get_current_run_tree
except Exception:  # pragma: no cover
    def traceable(*_args, **_kwargs):  # type: ignore
        def decorator(fn):
            return fn
        return decorator

    def get_current_run_tree():  # type: ignore
        return None


_metrics: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("request_metrics", default=None)


def start_request_metrics() -> dict:
    """Inisialisasi wadah metrik untuk satu request."""
    data = {
        "stages_ms": {},
        "llm_calls": [],
        "tokens": {"prompt": 0, "output": 0, "total": 0},
    }
    _metrics.set(data)
    return data


def get_request_metrics() -> Optional[dict]:
    return _metrics.get()


@contextmanager
def stage(name: str):
    """Mengukur durasi satu tahap pipeline (akumulatif bila dipanggil berulang)."""
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = (time.perf_counter() - start) * 1000
        data = _metrics.get()
        if data is not None:
            data["stages_ms"][name] = round(data["stages_ms"].get(name, 0.0) + elapsed, 1)


def usage_from_response(response: Any) -> dict:
    """Ambil token usage dari response google-genai (usage_metadata)."""
    usage = getattr(response, "usage_metadata", None)
    prompt = int(getattr(usage, "prompt_token_count", 0) or 0)
    output = int(getattr(usage, "candidates_token_count", 0) or 0)
    thoughts = int(getattr(usage, "thoughts_token_count", 0) or 0)
    total = int(getattr(usage, "total_token_count", 0) or (prompt + output + thoughts))
    return {"prompt": prompt, "output": output, "thoughts": thoughts, "total": total}


def record_llm_call(purpose: str, model: str, response: Any, latency_ms: float) -> dict:
    """Catat token usage satu panggilan LLM ke metrik request + run LangSmith aktif."""
    usage = usage_from_response(response)
    data = _metrics.get()
    if data is not None:
        data["llm_calls"].append(
            {"purpose": purpose, "model": model, "latency_ms": round(latency_ms, 1), **usage}
        )
        data["tokens"]["prompt"] += usage["prompt"]
        data["tokens"]["output"] += usage["output"] + usage["thoughts"]
        data["tokens"]["total"] += usage["total"]

    # Format usage_metadata yang dikenali LangSmith untuk perhitungan token/cost.
    run = get_current_run_tree()
    if run is not None:
        try:
            run.set(
                usage_metadata={
                    "input_tokens": usage["prompt"],
                    "output_tokens": usage["output"] + usage["thoughts"],
                    "total_tokens": usage["total"],
                }
            )
            run.metadata["ls_provider"] = "google_genai"
            run.metadata["ls_model_name"] = model
        except Exception:
            pass
    return usage


def record_llm_failure(purpose: str, model: str, latency_ms: float, error: Exception) -> None:
    """Catat panggilan LLM yang gagal (timeout/5xx) beserta waktu yang terbuang."""
    data = _metrics.get()
    if data is not None:
        data["llm_calls"].append(
            {
                "purpose": purpose,
                "model": model,
                "latency_ms": round(latency_ms, 1),
                "failed": True,
                "error": str(error)[:120],
            }
        )
