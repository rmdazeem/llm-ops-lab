# -*- coding: utf-8 -*-
"""
Project 1 — Alert-RCA assistant (RAG over runbooks + live alerts), fully on-prem via Ollama.

  python rca_assistant.py "why is api-service restarting on host-03?"
  python rca_assistant.py            # interactive

Flow:  question -> embed -> top-K runbook chunks (vector search)
       question -> keyword match -> matching alerts from data/alerts.json
       [runbook chunks + alerts + question] -> qwen2.5:7b -> JSON RCA with citations
"""
import json, re, sys, io, time, warnings
warnings.filterwarnings("ignore", message=".*AsyncOpenSearch.close.*")   # llama-index opensearch client never awaits close() at exit
from pathlib import Path

from llama_index.core import SimpleDirectoryReader, VectorStoreIndex, Settings, StorageContext, load_index_from_storage
from llama_index.core.node_parser import SentenceSplitter
from llama_index.llms.ollama import Ollama
from llama_index.embeddings.ollama import OllamaEmbedding

import os
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tracing                                          # Project 2: OpenTelemetry spans per question
import guardrails as gr                                 # Project 2d: input / retrieval / output guards
import alert_store                                      # Project 3a: alerts from OpenSearch (JSON fallback)
tracer = tracing.init("rca-assistant")
ROOT     = Path(__file__).resolve().parents[1]          # repo root, wherever it is cloned
CORPUS   = Path(os.environ.get("RCA_CORPUS", ROOT / "sample_corpus"))   # set RCA_CORPUS=E:\heal-llm-ops-lab\corpus for the private runbooks
ALERTS   = ROOT / "data" / "alerts.json"
INDEX_DIR = ROOT / "project1-rca-assistant" / f"index_store_{CORPUS.name}"
TOP_K = 3
PROMPT_VERSION = "v3"        # v3 = v2 prompt + guardrails (2d). bump when SYSTEM_PROMPT / TOP_K / chunking / json_mode changes — evals are compared per version
LAST_TRACE_ID = None         # set by answer(); lets the eval runner link a score to its trace

# v2: json_mode=True makes Ollama constrain the output to valid JSON (eval run v1: 1 of 7 answers was unparseable)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")   # P4: inside a pod "localhost" is the pod, not the host
Settings.llm = Ollama(model="qwen2.5:7b", base_url=OLLAMA_URL, request_timeout=600.0, temperature=0.1, json_mode=True)
Settings.embed_model = OllamaEmbedding(model_name="nomic-embed-text", base_url=OLLAMA_URL)
Settings.node_parser = SentenceSplitter(chunk_size=700, chunk_overlap=80)

# v1 -> v2 changes (from eval run 20260918T103812Z): root causes were generic / restated the question,
# and next_steps sometimes paraphrased instead of quoting the runbook.
SYSTEM_PROMPT = """You are an SRE assistant for an AIOps platform (Nomad, Consul, Docker, HAProxy,
OpenSearch, Percona MySQL, Keycloak, OpenTelemetry). Answer ONLY from the runbook excerpts and alerts given.
If the evidence is insufficient, say so — never invent hosts, commands or causes.

Rules for the answer:
- root_cause: name the failing COMPONENT and the MECHANISM (what broke and why), in one or two sentences.
  Never restate the alert or the question. Bad: "HAProxy backend is DOWN on host-01".
  Good: "The app-node service behind the backend is not listening on 9998 (or its health-check path changed), so HAProxy marks it DOWN."
- evidence: the specific alert lines you relied on, copied from RECENT ALERTS.
- next_steps: concrete commands or checks copied from the runbook excerpts, in the order to run them.
- citations: the runbook file names you actually used, each listed once.
- confidence: "high" only if both alerts and a runbook support the cause; "low" if evidence is insufficient.

Return STRICT JSON with exactly these keys:
{"root_cause": str, "evidence": [str], "next_steps": [str], "citations": [str], "confidence": "low|medium|high"}"""


VECTOR_STORE = os.environ.get("RCA_VECTOR_STORE", "opensearch")     # "opensearch" (3b, default) | "local" (in-memory files)
OS_URL = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")
KNN_INDEX = f"runbooks-{CORPUS.name}"                                 # one k-NN index per corpus
EMBED_DIM = 768                                                       # nomic-embed-text


def _load_docs():
    return SimpleDirectoryReader(str(CORPUS), recursive=True, required_exts=[".md", ".sh"],
                                 exclude=["REDACTION_MAP.json"]).load_data()


def _opensearch_index():
    """3b: vectors live in OpenSearch (knn_vector, Lucene HNSW, cosine). Built once; later runs just attach."""
    from llama_index.vector_stores.opensearch import OpensearchVectorClient, OpensearchVectorStore
    from opensearchpy import OpenSearch
    os_client = OpenSearch(OS_URL, timeout=30)
    exists = os_client.indices.exists(index=KNN_INDEX)
    count = os_client.count(index=KNN_INDEX)["count"] if exists else 0
    vclient = OpensearchVectorClient(
        OS_URL, KNN_INDEX, EMBED_DIM, embedding_field="embedding", text_field="content",
        method={"name": "hnsw", "space_type": "cosinesimil", "engine": "lucene",      # OpenSearch 3.x has no nmslib engine
                "parameters": {"ef_construction": 256, "m": 48}},
    )
    store = OpensearchVectorStore(vclient)
    if count:
        print(f"[index] attaching to OpenSearch k-NN index {KNN_INDEX} ({count} chunks)")
        return VectorStoreIndex.from_vector_store(store)
    print(f"[index] building OpenSearch k-NN index {KNN_INDEX} from {CORPUS} (first run only) ...")
    t = time.time()
    index = VectorStoreIndex.from_documents(_load_docs(), storage_context=StorageContext.from_defaults(vector_store=store),
                                            show_progress=True)
    print(f"[index] indexed in {time.time()-t:.0f}s -> OpenSearch {KNN_INDEX} ({os_client.count(index=KNN_INDEX)['count']} chunks)")
    return index


def build_or_load_index():
    global VECTOR_STORE
    if VECTOR_STORE == "opensearch":
        try:
            return _opensearch_index()
        except Exception as e:                                         # OpenSearch down -> fall back to local files
            print(f"[index] OpenSearch vector store unavailable ({type(e).__name__}: {str(e)[:80]}) -> local index")
            VECTOR_STORE = "local"                                     # the flag must follow the fallback: score normalisation,
                                                                       # trace attribute and /health all read it (bug found in 3d)
    if INDEX_DIR.exists():
        return load_index_from_storage(StorageContext.from_defaults(persist_dir=str(INDEX_DIR)))
    print(f"[index] building local index from {CORPUS} (first run only) ...")
    docs = _load_docs()
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
  global LAST_TRACE_ID
  with tracer.start_as_current_span("rca.question") as root:            # 1 trace per question
    LAST_TRACE_ID = format(root.get_span_context().trace_id, "032x")
    root.set_attribute("rca.corpus", CORPUS.name)
    root.set_attribute("rca.prompt_version", PROMPT_VERSION)

    # --- guard 1: input (secrets blocked, PII/IPs redacted BEFORE the question is logged or prompted) ---
    g_in = gr.input_guard(question)
    question = g_in["question"]                                          # redacted form is the only one that travels
    root.set_attribute("rca.question", question)
    root.set_attribute("guardrail.input", g_in["reason"])
    root.set_attribute("guardrail.redactions", g_in["redactions"])
    if g_in["action"] == "block":
        root.set_attribute("guardrail.action", "block_secret_request")
        data = gr.refusal("secret_request")
        root.set_attribute("rca.valid_json", True); root.set_attribute("rca.confidence", "low"); root.set_attribute("rca.alerts_matched", 0)
        return data, json.dumps(data), [], [], 0.0                       # no retrieval, no LLM call

    with tracer.start_as_current_span("rca.retrieve") as sp:            # embed question + vector search
        retriever = index.as_retriever(similarity_top_k=TOP_K)
        nodes = retriever.retrieve(question)
        if VECTOR_STORE == "opensearch":                                 # Lucene cosinesimil score = (1 + cos) / 2 -> back to raw cosine
            for n in nodes:                                              # so SIM_THRESHOLD keeps the same meaning on both backends
                n.score = 2 * n.score - 1
        sp.set_attribute("rca.top_k", TOP_K)
        sp.set_attribute("rca.vector_store", VECTOR_STORE)
        sp.set_attribute("rca.files", [n.metadata.get("file_name") for n in nodes])
        sp.set_attribute("rca.scores", [round(n.score, 3) for n in nodes])
        # --- guard 2: retrieval similarity threshold (refuse without spending an LLM call) ---
        g_ret = gr.retrieval_guard(nodes)
        sp.set_attribute("guardrail.retrieval", g_ret["reason"]); sp.set_attribute("guardrail.best_score", g_ret["best_score"])
    if g_ret["action"] == "block":
        root.set_attribute("guardrail.action", "block_low_similarity")
        data = gr.refusal("low_similarity")
        root.set_attribute("rca.valid_json", True); root.set_attribute("rca.confidence", "low"); root.set_attribute("rca.alerts_matched", 0)
        return data, json.dumps(data), nodes, [], 0.0
    runbook_ctx = "\n\n".join(
        f"--- runbook: {n.metadata.get('file_name')} (score {n.score:.2f}) ---\n{n.get_content()}" for n in nodes)

    with tracer.start_as_current_span("rca.match_alerts") as sp:        # 3a: OpenSearch query (host/service filters + fuzzy text)
        alerts, meta = alert_store.search_alerts(question)
        sp.set_attribute("rca.alerts_matched", len(alerts))
        sp.set_attribute("rca.alerts_source", meta["source"])
        sp.set_attribute("rca.alerts_hosts", meta["parsed"]["hosts"]); sp.set_attribute("rca.alerts_services", meta["parsed"]["services"])
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
        # --- guard 3: output contract (pydantic) + citations limited to files the model actually saw ---
        g_out = gr.validate_output(data, [n.metadata.get("file_name") for n in nodes])
        sp.set_attribute("guardrail.output", "ok" if g_out["ok"] else "invalid")
        sp.set_attribute("guardrail.citations_dropped", g_out["citations_dropped"])
        if g_out["ok"]:
            data = g_out["data"]
        else:
            sp.set_attribute("guardrail.output_errors", g_out["errors"][:5])
            data = None
        sp.set_attribute("rca.valid_json", data is not None)

    root.set_attribute("guardrail.action", "allow" if data is not None else "block_invalid_output")
    if data is None:
        data = gr.refusal("invalid_output")                             # never hand a raw/None answer to a user
    root.set_attribute("rca.valid_json", g_out["ok"])
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
