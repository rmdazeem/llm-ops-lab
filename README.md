# llm-ops-lab — on-prem RAG assistant for AIOps root-cause analysis

A hands-on LLMOps lab built by an SRE: a fully offline assistant that reads infrastructure alerts and
operations runbooks and returns a **cited, schema-checked root-cause analysis** — the kind of thing a bank can
run inside its own data centre with no data leaving the building.

Runs on a laptop CPU (no GPU) with open-weight models via [Ollama](https://ollama.com).

## What it does

```
question ─► nomic-embed-text ─► vector search over runbook chunks ─► top-3 chunks ─┐
question ─► keyword match over alerts.json ─► matching alerts ────────────────────┼─► qwen2.5:7b ─► strict JSON
                                                                                  ┘   {root_cause, evidence,
                                                                                       next_steps, citations,
                                                                                       confidence}
```

Example (CPU, ~3 min):

```
>>> why is api-service restarting on host-03?
[retrieval] ['nomad_consul_runbook.md', 'percona_redis_runbook.md', 'haproxy_runbook.md']   [alerts matched] 8
ROOT CAUSE  : Nomad allocation restarting on host-03 due to NFS mount and Redis connection errors
CONFIDENCE  : medium
EVIDENCE    : - NFS mount unreachable on host-03 (nfs_rpc_timeouts=6, threshold 1)
              - CRITICAL Redis connection refused on host-03 (conn_errors_5m=23, threshold 5)
NEXT STEPS  : 1. docker ps | grep redis ...  2. redis-cli -h host-03 -p 6379 cluster info ...  3. nomad alloc restart <alloc>
CITATIONS   : ['percona_redis_runbook.md', 'nomad_consul_runbook.md']
```

Out-of-scope questions ("why is the Windows Power BI server slow?") return `confidence: low` and
`Insufficient evidence` instead of an invented cause.

## Layout

| Path | Purpose |
|---|---|
| `project1-rca-assistant/rca_assistant.py` | The assistant: index build, retrieval, alert matching, prompt, JSON parsing |
| `sample_corpus/` | Generic runbooks (HAProxy, OpenSearch, Nomad/Consul, Percona/Redis, TLS/Keycloak/OTel) written for this lab |
| `data/alerts.json` | 300 synthetic alerts across 8 hosts / 15 services (`tools/gen_alerts.py`) |
| `tools/redact_corpus.py` | Pseudonymises a private runbook set (IPs, hostnames, org names, secrets → stable fakes) so real docs can be used locally without ever being committed |

The private corpus used for real testing is **not** in this repository; `RCA_CORPUS=<path>` points the assistant at it.

## Run it

```bash
ollama pull qwen2.5:7b && ollama pull nomic-embed-text
pip install llama-index-core llama-index-llms-ollama llama-index-embeddings-ollama
python tools/gen_alerts.py
python project1-rca-assistant/rca_assistant.py            # interactive; first run builds the index
python project1-rca-assistant/rca_assistant.py "OpenSearch cluster is RED on host-07, what do I check first?"
```

## Findings from the first eval pass

- The model was faithful to the runbooks; one *runbook* was stale (systemd commands for a containerised service) and
  the assistant reproduced it confidently. **RAG quality is corpus quality** — the fix was the document, not the prompt.
- Vector search always returns the nearest chunks even when nothing is relevant; the model refused correctly, but a
  similarity-score threshold belongs in front of the prompt (Project 2).
- Simple keyword alert matching is brittle (`host01` vs `host-01`); Project 3 replaces it with a query tool.

## Roadmap

- **Project 2** — observability: OpenTelemetry GenAI spans per call (model, tokens, latency) → Collector → OpenSearch → Grafana;
  eval harness (faithfulness, citation accuracy, hallucination rate); guardrails (PII/IP redaction, schema, score threshold).
- **Project 3** — agentic triage via an MCP tool server (alerts / events / runbooks) with human-in-the-loop approval.
- **Project 4** — the whole stack on Kubernetes (k3s + Helm).
