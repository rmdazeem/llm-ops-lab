# -*- coding: utf-8 -*-
"""(Re)build the OpenSearch `llm-evals` index from evals/results.sqlite — flags as 0/1 so Grafana can average them."""
import json, sqlite3, urllib.request, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]; OS = "http://localhost:9200"; IDX = "llm-evals"
MAPPING = {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {"properties": {
    "@timestamp": {"type": "date"}, "run_id": {"type": "keyword"}, "qid": {"type": "keyword"}, "question": {"type": "text"},
    "trace_id": {"type": "keyword"}, "prompt_version": {"type": "keyword"}, "judge_version": {"type": "keyword"},
    "model": {"type": "keyword"}, "corpus": {"type": "keyword"},
    "must_refuse": {"type": "integer"}, "schema_ok": {"type": "integer"}, "citation_ok": {"type": "integer"},
    "refusal_ok": {"type": "integer"}, "hallucinated": {"type": "integer"},
    "correctness": {"type": "integer"}, "faithfulness": {"type": "integer"}, "latency_s": {"type": "float"}}}}
def http(m, path, body=None, nd=False):
    data = body.encode() if isinstance(body, str) else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(OS + path, data=data, method=m, headers={"Content-Type": "application/x-ndjson" if nd else "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r: return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")
http("DELETE", f"/{IDX}"); print("index recreated:", http("PUT", f"/{IDX}", MAPPING)[0])
con = sqlite3.connect(ROOT / "evals" / "results.sqlite")
rows = con.execute("""select x.run_id, r.ts, r.prompt_version, r.judge_version, r.model, r.corpus, x.qid, x.question, x.trace_id,
    x.must_refuse, x.schema_ok, x.citation_ok, x.refusal_ok, x.correctness, x.faithfulness, x.hallucinated, x.latency_s
    from results x join runs r on r.run_id = x.run_id""").fetchall()
body = ""
for r in rows:
    doc = {"@timestamp": r[1], "run_id": r[0], "prompt_version": r[2], "judge_version": r[3] or "j1", "model": r[4], "corpus": r[5],
           "qid": r[6], "question": r[7], "trace_id": r[8], "must_refuse": r[9], "schema_ok": r[10], "citation_ok": r[11],
           "refusal_ok": r[12], "correctness": r[13], "faithfulness": r[14], "hallucinated": r[15], "latency_s": r[16]}
    body += json.dumps({"index": {"_index": IDX, "_id": f"{r[0]}-{r[6]}"}}) + "\n" + json.dumps(doc) + "\n"
code, resp = http("POST", "/_bulk?refresh=true", body, nd=True)
print(f"loaded {len(rows)} results ({code}, errors={resp.get('errors')})")
for b in http("POST", f"/{IDX}/_search", {"size": 0, "aggs": {"v": {"terms": {"field": "prompt_version"}, "aggs": {"h": {"avg": {"field": "hallucinated"}}}}}})[1]["aggregations"]["v"]["buckets"]:
    print(f"  prompt {b['key']}: hallucination rate {b['h']['value']:.0%}  (n={b['doc_count']})")
