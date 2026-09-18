# -*- coding: utf-8 -*-
"""
Project 3c — the RCA assistant as an HTTP service (FastAPI).

  uvicorn service.app:app --host 127.0.0.1 --port 8080          (run from the repo root)

  POST /rca      {"question": "..."}   -> answer + trace_id + guardrail action + timings
  GET  /health   -> dependencies (ollama, opensearch, k-NN index, alerts index) — what a load balancer / Nomad check probes
  GET  /metrics  -> Prometheus text format: questions, blocked, errors, latency
  GET  /docs     -> OpenAPI UI (FastAPI generates it)

Same tracing and guardrails as the CLI — answer() is reused, not copied.
"""
import sys, time, threading, io
from pathlib import Path
from collections import Counter

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
import urllib.request, json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "project1-rca-assistant"))
import rca_assistant as rca                                    # noqa: E402  (loads models, tracing, guardrails)
from opentelemetry import trace                                 # noqa: E402

app = FastAPI(title="RCA assistant", version="0.3", description="On-prem RAG root-cause assistant with tracing, evals and guardrails")
_index = None
_lock = threading.Lock()                                        # one LLM call at a time on a CPU laptop
_stats = Counter()
_lat = []                                                       # last 200 latencies (s)
_started = time.time()


class RCARequest(BaseModel):
    question: str = Field(min_length=5, max_length=1000)


class RCAResponse(BaseModel):
    root_cause: str
    evidence: list[str]
    next_steps: list[str]
    citations: list[str]
    confidence: str
    trace_id: str
    guardrail_action: str
    retrieved: list[str]
    alerts_matched: int
    latency_s: float


@app.on_event("startup")
def _startup():
    global _index
    _index = rca.build_or_load_index()                          # attaches to OpenSearch k-NN (or local fallback)


def _get(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except Exception as e:
        return 0, {"error": type(e).__name__}


@app.get("/health")
def health():
    code, v = _get("http://localhost:11434/api/version")
    ollama = {"ok": code == 200, "version": v.get("version")}
    code, ps = _get("http://localhost:11434/api/ps")
    ollama["loaded_models"] = [m["name"] for m in ps.get("models", [])] if code == 200 else []
    code, h = _get(f"{rca.OS_URL}/_cluster/health")
    opensearch = {"ok": code == 200, "status": h.get("status")}
    code, c = _get(f"{rca.OS_URL}/{rca.KNN_INDEX}/_count")
    knn = {"ok": code == 200, "chunks": c.get("count")}
    code, c = _get(f"{rca.OS_URL}/alerts/_count")
    alerts = {"ok": code == 200, "docs": c.get("count")}
    ok = ollama["ok"] and opensearch["ok"] and knn["ok"]
    body = {"status": "ok" if ok else "degraded", "uptime_s": round(time.time() - _started),
            "prompt_version": rca.PROMPT_VERSION, "vector_store": rca.VECTOR_STORE,
            "ollama": ollama, "opensearch": opensearch, "knn_index": knn, "alerts_index": alerts}
    if not ok:
        raise HTTPException(status_code=503, detail=body)      # 503 so a probe/LB takes the instance out
    return body


@app.post("/rca", response_model=RCAResponse)
def rca_endpoint(req: RCARequest):
    with _lock:
        t = time.time()
        try:
            data, raw, nodes, alerts, _ = rca.answer(_index, req.question)
        except Exception as e:
            _stats["errors"] += 1
            raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
        latency = time.time() - t
    _lat.append(latency); del _lat[:-200]
    action = _last_guardrail_action()
    _stats["questions"] += 1
    _stats[f"guardrail_{action}"] += 1
    return RCAResponse(**data, trace_id=rca.LAST_TRACE_ID or "", guardrail_action=action,
                       retrieved=[n.metadata.get("file_name") for n in nodes], alerts_matched=len(alerts),
                       latency_s=round(latency, 1))


def _last_guardrail_action():
    """Read the action the root span recorded (kept simple: last line of the JSONL trace file)."""
    try:
        lines = io.open(rca.tracing.TRACE_FILE, encoding="utf-8").read().splitlines()
        for line in reversed(lines[-8:]):
            d = json.loads(line)
            if d["name"] == "rca.question":
                return d["attributes"].get("guardrail.action", "unknown")
    except Exception:
        pass
    return "unknown"


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    p95 = sorted(_lat)[int(len(_lat) * 0.95) - 1] if len(_lat) >= 2 else (_lat[0] if _lat else 0)
    out = ["# HELP rca_questions_total Questions answered", "# TYPE rca_questions_total counter",
           f"rca_questions_total {_stats['questions']}",
           "# TYPE rca_errors_total counter", f"rca_errors_total {_stats['errors']}",
           "# TYPE rca_guardrail_total counter"]
    out += [f'rca_guardrail_total{{action="{k[10:]}"}} {v}' for k, v in _stats.items() if k.startswith("guardrail_")]
    out += ["# TYPE rca_latency_seconds_p95 gauge", f"rca_latency_seconds_p95 {p95:.1f}",
            "# TYPE rca_uptime_seconds gauge", f"rca_uptime_seconds {time.time() - _started:.0f}"]
    return "\n".join(out) + "\n"
