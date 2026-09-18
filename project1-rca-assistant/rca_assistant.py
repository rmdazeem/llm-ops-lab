# -*- coding: utf-8 -*-
"""
Project 1 — Alert-RCA assistant (RAG over runbooks + live alerts), fully on-prem via Ollama.

  python rca_assistant.py "why is api-service restarting on host-03?"
  python rca_assistant.py            # interactive

Flow:  question -> embed -> top-K runbook chunks (vector search)
       question -> keyword match -> matching alerts from data/alerts.json
       [runbook chunks + alerts + question] -> qwen2.5:7b -> JSON RCA with citations
"""
import json, re, sys, io, time
from pathlib import Path

from llama_index.core import SimpleDirectoryReader, VectorStoreIndex, Settings, StorageContext, load_index_from_storage
from llama_index.core.node_parser import SentenceSplitter
from llama_index.llms.ollama import Ollama
from llama_index.embeddings.ollama import OllamaEmbedding

import os
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tracing                                          # Project 2: OpenTelemetry spans per question
tracer = tracing.init("rca-assistant")
ROOT     = Path(__file__).resolve().parents[1]          # repo root, wherever it is cloned
CORPUS   = Path(os.environ.get("RCA_CORPUS", ROOT / "sample_corpus"))   # set RCA_CORPUS=E:\heal-llm-ops-lab\corpus for the private runbooks
ALERTS   = ROOT / "data" / "alerts.json"
INDEX_DIR = ROOT / "project1-rca-assistant" / f"index_store_{CORPUS.name}"
TOP_K = 3

Settings.llm = Ollama(model="qwen2.5:7b", request_timeout=600.0, temperature=0.1)
Settings.embed_model = OllamaEmbedding(model_name="nomic-embed-text")
Settings.node_parser = SentenceSplitter(chunk_size=700, chunk_overlap=80)

SYSTEM_PROMPT = """You are an SRE assistant for an AIOps platform (Nomad, Consul, Docker, HAProxy,
OpenSearch, Percona MySQL, Keycloak, OpenTelemetry). Answer ONLY from the runbook excerpts and alerts given.
If the evidence is insufficient, say so — never invent hosts, commands or causes.
Return STRICT JSON with exactly these keys:
{"root_cause": str, "evidence": [str], "next_steps": [str], "citations": [str], "confidence": "low|medium|high"}
citations = the runbook file names you used. next_steps = concrete commands or checks, in order."""


def build_or_load_index():
    if INDEX_DIR.exists():
        return load_index_from_storage(StorageContext.from_defaults(persist_dir=str(INDEX_DIR)))
    print(f"[index] building from {CORPUS} (first run only) ...")
    docs = SimpleDirectoryReader(str(CORPUS), recursive=True, required_exts=[".md", ".sh"],
                                 exclude=["REDACTION_MAP.json"]).load_data()
    t = time.time()
    index = VectorStoreIndex.from_documents(docs, show_progress=True)
    index.storage_context.persist(persist_dir=str(INDEX_DIR))
    print(f"[index] {len(docs)} docs indexed in {time.time()-t:.0f}s -> {INDEX_DIR}")
    return index


def matching_alerts(question, limit=8):
    """Cheap keyword filter over alerts.json — Project 3 replaces this with an OpenSearch query tool."""
    alerts = json.load(io.open(ALERTS, encoding="utf-8"))
    words = {w for w in re.findall(r"[a-z0-9\-]+", question.lower()) if len(w) > 2}
    def score(a):
        hay = " ".join(str(v) for v in a.values()).lower()
        return sum(1 for w in words if w in hay)
    ranked = sorted(alerts, key=lambda a: (score(a), a["timestamp"]), reverse=True)
    return [a for a in ranked[:limit] if score(a) > 0]


def answer(index, question):
  with tracer.start_as_current_span("rca.question") as root:            # 1 trace per question
    root.set_attribute("rca.question", question)
    root.set_attribute("rca.corpus", CORPUS.name)

    with tracer.start_as_current_span("rca.retrieve") as sp:            # embed question + vector search
        retriever = index.as_retriever(similarity_top_k=TOP_K)
        nodes = retriever.retrieve(question)
        sp.set_attribute("rca.top_k", TOP_K)
        sp.set_attribute("rca.files", [n.metadata.get("file_name") for n in nodes])
        sp.set_attribute("rca.scores", [round(n.score, 3) for n in nodes])
    runbook_ctx = "\n\n".join(
        f"--- runbook: {n.metadata.get('file_name')} (score {n.score:.2f}) ---\n{n.get_content()}" for n in nodes)

    with tracer.start_as_current_span("rca.match_alerts") as sp:        # keyword match over alerts.json
        alerts = matching_alerts(question)
        sp.set_attribute("rca.alerts_matched", len(alerts))
    alert_ctx = "\n".join(f"- [{a['timestamp']}] {a['severity'].upper()} {a['message']} (status {a['status']})"
                          for a in alerts) or "- (no matching alerts in the last 7 days)"

    prompt = (f"{SYSTEM_PROMPT}\n\n### RUNBOOK EXCERPTS\n{runbook_ctx}\n\n### RECENT ALERTS\n{alert_ctx}\n\n"
              f"### QUESTION\n{question}\n\n### JSON ANSWER\n")

    with tracer.start_as_current_span("gen_ai.completion") as sp:       # the LLM call — GenAI semantic conventions
        sp.set_attribute("gen_ai.system", "ollama")
        sp.set_attribute("gen_ai.operation.name", "text_completion")
        sp.set_attribute("gen_ai.request.model", Settings.llm.model)
        sp.set_attribute("gen_ai.request.temperature", Settings.llm.temperature)
        t = time.time()
        resp = Settings.llm.complete(prompt)
        latency = time.time() - t
        raw = resp.text
        usage = resp.raw if isinstance(resp.raw, dict) else {}            # Ollama returns token counts here
        in_tok, out_tok = usage.get("prompt_eval_count"), usage.get("eval_count")
        if in_tok is not None:  sp.set_attribute("gen_ai.usage.input_tokens", in_tok)
        if out_tok is not None: sp.set_attribute("gen_ai.usage.output_tokens", out_tok)
        if out_tok and usage.get("eval_duration"):
            sp.set_attribute("gen_ai.tokens_per_s", round(out_tok / (usage["eval_duration"] / 1e9), 1))
        sp.set_attribute("gen_ai.response.finish_reason", usage.get("done_reason", "unknown"))

        m = re.search(r"\{.*\}", raw, re.S)                  # tolerate ```json fences / chatter
        try:
            data = json.loads(m.group(0)) if m else None
        except json.JSONDecodeError:
            data = None
        sp.set_attribute("rca.valid_json", data is not None)

    root.set_attribute("rca.valid_json", data is not None)
    root.set_attribute("rca.confidence", (data or {}).get("confidence", "n/a"))
    root.set_attribute("rca.alerts_matched", len(alerts))
    return data, raw, nodes, alerts, latency


def show(question, data, raw, nodes, alerts, latency):
    print(f"\n=== {question}")
    print(f"[retrieval] {[n.metadata.get('file_name') for n in nodes]}   [alerts matched] {len(alerts)}   [llm] {latency:.0f}s")
    if data is None:
        print("[warn] model did not return valid JSON — raw output:\n", raw); return
    print(f"ROOT CAUSE  : {data.get('root_cause')}")
    print(f"CONFIDENCE  : {data.get('confidence')}")
    print("EVIDENCE    :"); [print(f"  - {e}") for e in data.get("evidence", [])]
    print("NEXT STEPS  :"); [print(f"  {i}. {s}") for i, s in enumerate(data.get("next_steps", []), 1)]
    print(f"CITATIONS   : {data.get('citations')}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    index = build_or_load_index()
    if len(sys.argv) > 1:
        q = " ".join(sys.argv[1:]); show(q, *answer(index, q))
    else:
        print(f"RCA assistant [{CORPUS.name}] — type a question, or 'exit'")
        while True:
            q = input("\n>>> ").strip()
            if q.lower() in ("exit", "quit", ""): break
            show(q, *answer(index, q))
