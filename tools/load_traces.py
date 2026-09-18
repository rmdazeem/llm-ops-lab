# -*- coding: utf-8 -*-
"""
Load data/traces.jsonl into OpenSearch index `llm-traces` (idempotent: _id = span_id).

  python tools/load_traces.py                 # load, then print a p95 latency / token summary
  OPENSEARCH_URL=http://localhost:9200        # default

Stdlib only (urllib) so there is nothing to install. In production this is what the
OTel Collector's opensearch exporter does for you; doing it by hand once shows what lands in the index.
"""
import json, io, os, sys, urllib.request
from pathlib import Path

OS_URL = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")
INDEX  = "llm-traces"
TRACES = Path(__file__).resolve().parents[1] / "data" / "traces.jsonl"

MAPPING = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "properties": {
            "@timestamp":  {"type": "date", "format": "epoch_millis"},
            "end_ts":      {"type": "date", "format": "epoch_millis"},
            "trace_id":    {"type": "keyword"},
            "span_id":     {"type": "keyword"},
            "parent_id":   {"type": "keyword"},
            "name":        {"type": "keyword"},
            "status":      {"type": "keyword"},
            "duration_ms": {"type": "float"},
            "attributes": {"properties": {
                "gen_ai": {"properties": {
                    "request":  {"properties": {"model": {"type": "keyword"}, "temperature": {"type": "float"}}},
                    "usage":    {"properties": {"input_tokens": {"type": "long"}, "output_tokens": {"type": "long"}}},
                    "tokens_per_s": {"type": "float"},
                    "system":   {"type": "keyword"},
                    "response": {"properties": {"finish_reason": {"type": "keyword"}}},
                }},
                "guardrail": {"properties": {                       # 2d: explicit keyword mapping — dynamic mapping made these `text`
                    "action": {"type": "keyword"}, "input": {"type": "keyword"}, "retrieval": {"type": "keyword"},
                    "output": {"type": "keyword"}, "redactions": {"type": "integer"}, "best_score": {"type": "float"},
                    "citations_dropped": {"type": "integer"}, "output_errors": {"type": "keyword"},
                }},
                "rca": {"properties": {
                    "question":       {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": 512}}},
                    "corpus":         {"type": "keyword"},
                    "confidence":     {"type": "keyword"},
                    "valid_json":     {"type": "boolean"},
                    "alerts_matched": {"type": "integer"},
                    "top_k":          {"type": "integer"},
                    "files":          {"type": "keyword"},
                    "scores":         {"type": "float"},
                }},
            }},
        }
    },
}


def http(method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/x-ndjson" if isinstance(body, str) else "application/json"
    req = urllib.request.Request(OS_URL + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def ensure_index():
    if "--recreate" in sys.argv:                       # mapping changed -> drop and rebuild (JSONL is the source of truth)
        http("DELETE", f"/{INDEX}")
    code, _ = http("HEAD", f"/{INDEX}")
    if code == 200:
        return "exists"
    code, resp = http("PUT", f"/{INDEX}", MAPPING)
    return "created" if code == 200 else f"error {code}: {resp}"


def to_doc(span):
    doc = dict(span)
    doc["@timestamp"] = int(span["start"] / 1e6)          # ns -> ms
    doc["end_ts"] = int(span["end"] / 1e6)
    doc.pop("start"); doc.pop("end")
    return doc


def bulk_load():
    lines = [l for l in io.open(TRACES, encoding="utf-8") if l.strip()]
    body = ""
    for l in lines:
        span = json.loads(l)
        body += json.dumps({"index": {"_index": INDEX, "_id": span["span_id"]}}) + "\n"
        body += json.dumps(to_doc(span)) + "\n"
    code, resp = http("POST", "/_bulk?refresh=true", body)
    items = resp.get("items", [])
    created = sum(1 for i in items if i["index"].get("result") == "created")
    updated = sum(1 for i in items if i["index"].get("result") == "updated")
    failed  = [i["index"] for i in items if i["index"].get("status", 200) >= 300]
    print(f"[bulk] spans in file: {len(lines)}   created: {created}   updated (re-run): {updated}   failed: {len(failed)}")
    for f in failed[:3]:
        print("   ", json.dumps(f)[:300])


def summary():
    q = {
        "size": 0,
        "query": {"term": {"name": "gen_ai.completion"}},
        "aggs": {
            "latency_ms": {"percentiles": {"field": "duration_ms", "percents": [50, 95]}},
            "input_tokens":  {"avg": {"field": "attributes.gen_ai.usage.input_tokens"}},
            "output_tokens": {"avg": {"field": "attributes.gen_ai.usage.output_tokens"}},
            "tokens_per_s":  {"avg": {"field": "attributes.gen_ai.tokens_per_s"}},
            "valid_json":    {"terms": {"field": "attributes.rca.valid_json"}},
        },
    }
    code, r = http("POST", f"/{INDEX}/_search", q)
    a = r.get("aggregations", {})
    n = r.get("hits", {}).get("total", {}).get("value", 0)
    print(f"\n[summary] gen_ai.completion spans: {n}")
    if n:
        p = a["latency_ms"]["values"]
        print(f"  latency  p50 {p['50.0']/1000:.0f}s   p95 {p['95.0']/1000:.0f}s")
        print(f"  tokens   in {a['input_tokens']['value']:.0f} avg   out {a['output_tokens']['value']:.0f} avg   {a['tokens_per_s']['value']:.1f} tok/s")
        print(f"  valid_json: {[(b['key_as_string'], b['doc_count']) for b in a['valid_json']['buckets']]}")
    code, r = http("POST", f"/{INDEX}/_search", {"size": 0, "aggs": {"by_status": {"terms": {"field": "status"}}}})
    print(f"  all spans by status: {[(b['key'], b['doc_count']) for b in r['aggregations']['by_status']['buckets']]}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    code, info = http("GET", "/")
    if code != 200:
        sys.exit(f"OpenSearch not reachable at {OS_URL} ({code})")
    print(f"[opensearch] {info.get('version', {}).get('number')} at {OS_URL}   index {INDEX}: {ensure_index()}")
    bulk_load()
    summary()
