# -*- coding: utf-8 -*-
"""
Project 3d — MCP tool server for AIOps triage.

Exposes four tools over the Model Context Protocol (stdio). Three READ tools and one WRITE tool:
  get_alerts(host, service, window, limit)   OpenSearch `alerts` query
  search_runbooks(query, k)                  k-NN vector search over `runbooks-<corpus>` (raw query, scores normalised to cosine)
  run_rca(question)                          calls the FastAPI service POST /rca (tracing + guardrails included)
  draft_ticket(title, body, severity)        WRITE: stores a ticket in OpenSearch `tickets` — the agent must get human approval first

Run standalone for a smoke test:   python mcp_server/tools.py --test
As an MCP server (stdio):          python mcp_server/tools.py
"""
import json, os, sys, time, urllib.request, asyncio, logging
logging.getLogger("opensearch").setLevel(logging.WARNING); logging.getLogger("httpx").setLevel(logging.WARNING)
from datetime import datetime, timezone
from pathlib import Path
from mcp.server.mcpserver import MCPServer          # mcp 2.x (was FastMCP in 1.x)
from opensearchpy import OpenSearch
import ollama

OS_URL = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")
RCA_URL = os.environ.get("RCA_URL", "http://127.0.0.1:8080")
CORPUS = Path(os.environ.get("RCA_CORPUS", Path(__file__).resolve().parents[1] / "sample_corpus")).name
KNN_INDEX = f"runbooks-{CORPUS}"
EMBED_MODEL = "nomic-embed-text"

mcp = MCPServer("aiops-triage-tools")
_os = OpenSearch(OS_URL, timeout=30)


def _window(w: str) -> str:
    """Accept '1h', '24h', '7d', 'now-1h' — the LLM will pass any of these; OpenSearch only accepts 'now-1h'."""
    w = (w or "now-30d").strip().lower()
    return w if w.startswith("now") else f"now-{w}"


@mcp.tool()
def get_alerts(host: str = "", service: str = "", window: str = "now-30d", limit: int = 10) -> dict:
    """Open/acknowledged alerts from OpenSearch, newest first. Filter by host (e.g. host-01) and/or service (e.g. haproxy).
    window is relative, e.g. '1h', '24h', '7d' (default 30d). If the window has no alerts it is widened to 30d and 'window_used' says so."""
    must = [{"range": {"@timestamp": {"gte": _window(window)}}}]
    if host:    must.append({"term": {"host": host.lower().replace("host", "host-").replace("--", "-")}})
    if service: must.append({"term": {"service": service.lower()}})
    body = {"size": limit, "query": {"bool": {"must": must, "must_not": [{"term": {"status": "resolved"}}]}},
            "sort": [{"@timestamp": "desc"}], "_source": ["timestamp", "severity", "host", "service", "alert_name", "message", "status"]}
    hits = [h["_source"] for h in _os.search(index="alerts", body=body)["hits"]["hits"]]
    used = _window(window)
    if not hits and used != "now-30d":                                       # widen once so the agent sees history, not "nothing"
        must[0] = {"range": {"@timestamp": {"gte": "now-30d"}}}; used = "now-30d (widened)"
        hits = [h["_source"] for h in _os.search(index="alerts", body=body)["hits"]["hits"]]
    return {"window_used": used, "count": len(hits), "alerts": hits}


@mcp.tool()
def search_runbooks(query: str, k: int = 3) -> list[dict]:
    """Semantic search over the runbooks (k-NN). Returns file name, cosine score and the chunk text."""
    vec = ollama.embed(model=EMBED_MODEL, input=query)["embeddings"][0]
    body = {"size": k, "query": {"knn": {"embedding": {"vector": vec, "k": k}}}, "_source": ["metadata", "content"]}
    out = []
    for h in _os.search(index=KNN_INDEX, body=body)["hits"]["hits"]:
        out.append({"file": h["_source"].get("metadata", {}).get("file_name"),
                    "score": round(2 * h["_score"] - 1, 3),                     # Lucene cosinesimil -> raw cosine
                    "text": h["_source"]["content"][:1500]})
    return out


@mcp.tool()
def run_rca(question: str) -> dict:
    """Ask the RCA assistant service (RAG + guardrails). Slow on CPU (minutes). Returns root_cause, evidence, next_steps, citations, confidence, trace_id."""
    req = urllib.request.Request(f"{RCA_URL}/rca", data=json.dumps({"question": question}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read())


@mcp.tool()
def draft_ticket(title: str, body: str, severity: str = "medium", host: str = "", trace_id: str = "") -> dict:
    """WRITE ACTION: create a draft incident ticket. Callers MUST obtain human approval before invoking this tool."""
    if not _os.indices.exists(index="tickets"):
        _os.indices.create(index="tickets", body={"settings": {"number_of_replicas": 0}, "mappings": {"properties": {
            "@timestamp": {"type": "date"}, "title": {"type": "text"}, "body": {"type": "text"}, "severity": {"type": "keyword"},
            "host": {"type": "keyword"}, "trace_id": {"type": "keyword"}, "status": {"type": "keyword"}, "created_by": {"type": "keyword"}}}})
    doc = {"@timestamp": datetime.now(timezone.utc).isoformat(), "title": title, "body": body, "severity": severity,
           "host": host, "trace_id": trace_id, "status": "draft", "created_by": "triage-agent"}
    r = _os.index(index="tickets", body=doc, refresh=True)
    return {"ticket_id": r["_id"], "status": "draft", "title": title}


if __name__ == "__main__":
    if "--test" in sys.argv:
        print("get_alerts(host-01, 1h):", get_alerts(host="host-01", window="1h")["window_used"], get_alerts(host="host-01")["count"], "alerts")
        rb = search_runbooks("HAProxy backend down", k=2)
        print("search_runbooks:", [(r["file"], r["score"]) for r in rb])
        print("tools registered:", [t.name for t in asyncio.run(mcp.list_tools())])
    else:
        mcp.run(transport="stdio")
