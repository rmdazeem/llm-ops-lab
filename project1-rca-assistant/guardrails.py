# -*- coding: utf-8 -*-
"""
Project 2d — guardrails around the RCA assistant. Four checks, each cheap and deterministic:

  1. input_guard(question)      block secret requests; redact IPs / emails / long digit strings before anything is logged or prompted
  2. retrieval_guard(nodes)     if the best runbook chunk is below SIM_THRESHOLD, refuse WITHOUT calling the LLM
  3. validate_output(data, …)   pydantic contract for the answer; citations must be files that were actually retrieved
  4. refusal(reason)            one standard refusal shape (confidence=low) so evals and dashboards can count it

Every decision is returned as a small dict so the caller can put it on the OpenTelemetry span
(guardrail.action, guardrail.reason, guardrail.redactions) -> Grafana panel "blocked by guardrail".
"""
import re
from typing import List, Literal
from pydantic import BaseModel, Field, ValidationError, field_validator

SIM_THRESHOLD = 0.55      # observed: relevant questions score 0.60-0.70, the Power BI trap 0.51 — tune per embedding model

# --- 1. input guard --------------------------------------------------------------------------
SECRET_RE = re.compile(r"\b(password|passwd|pwd|passphrase|secret|token|api[_-]?key|private[_ -]?key|credential)s?\b", re.I)
REDACTIONS = [
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),                      # IPv4
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[EMAIL]"),                        # email
    (re.compile(r"(?<![\w-])\d[\d\s-]{9,}\d(?![\w-])"), "[NUMBER]"),            # 11+ digit strings incl. spaces/dashes: account/card/phone
]


def input_guard(question: str) -> dict:
    """Returns {"action": "allow"|"block", "reason": str, "question": redacted, "redactions": n}."""
    if SECRET_RE.search(question):
        return {"action": "block", "reason": "secret_request", "question": question, "redactions": 0}
    redacted, n = question, 0
    for rx, tag in REDACTIONS:
        redacted, k = rx.subn(tag, redacted)
        n += k
    return {"action": "allow", "reason": "pii_redacted" if n else "clean", "question": redacted, "redactions": n}


# --- 2. retrieval guard ----------------------------------------------------------------------
def retrieval_guard(nodes, threshold: float = SIM_THRESHOLD) -> dict:
    best = max((n.score or 0.0) for n in nodes) if nodes else 0.0
    if best < threshold:
        return {"action": "block", "reason": "low_similarity", "best_score": round(best, 3)}
    return {"action": "allow", "reason": "ok", "best_score": round(best, 3)}


# --- 3. output contract ----------------------------------------------------------------------
class RCAAnswer(BaseModel):
    root_cause: str = Field(min_length=10)
    evidence: List[str]
    next_steps: List[str]
    citations: List[str]
    confidence: Literal["low", "medium", "high"]

    @field_validator("confidence", mode="before")
    @classmethod
    def _lower(cls, v):
        return str(v).lower().strip() if v is not None else v


def validate_output(data: dict, retrieved_files: List[str]) -> dict:
    """Returns {"ok": bool, "data": cleaned dict|None, "errors": [...], "citations_dropped": n}."""
    if not isinstance(data, dict):
        return {"ok": False, "data": None, "errors": ["not a JSON object"], "citations_dropped": 0}
    try:
        ans = RCAAnswer(**data)
    except ValidationError as e:
        return {"ok": False, "data": None, "errors": [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()], "citations_dropped": 0}
    allowed = set(retrieved_files)
    kept = [c for c in dict.fromkeys(ans.citations) if c in allowed]      # dedupe + only files the model actually saw
    dropped = len(ans.citations) - len(kept)
    out = ans.model_dump(); out["citations"] = kept
    return {"ok": True, "data": out, "errors": [], "citations_dropped": dropped}


# --- 4. standard refusal ---------------------------------------------------------------------
REFUSAL_TEXT = {
    "secret_request": "Insufficient evidence: credentials and secrets are never available to this assistant.",
    "low_similarity": "Insufficient evidence: no runbook covers this question closely enough to answer safely.",
    "invalid_output": "Insufficient evidence: the model did not return a valid answer.",
}


def refusal(reason: str) -> dict:
    return {"root_cause": REFUSAL_TEXT.get(reason, "Insufficient evidence."), "evidence": [], "next_steps": [],
            "citations": [], "confidence": "low"}
