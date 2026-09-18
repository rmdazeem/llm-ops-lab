# -*- coding: utf-8 -*-
"""Load data/alerts.json into OpenSearch index `alerts` (idempotent: _id = alert_id).  python tools/load_alerts.py [--recreate]"""
import json, io, sys, os
from pathlib import Path
from opensearchpy import OpenSearch, helpers

OS_URL = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")
INDEX = "alerts"
SRC = Path(__file__).resolve().parents[1] / "data" / "alerts.json"

MAPPING = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {"properties": {
        "@timestamp":    {"type": "date"},
        "alert_id":      {"type": "keyword"},
        "severity":      {"type": "keyword"},
        "alert_name":    {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
        "host":          {"type": "keyword"},
        "role":          {"type": "keyword"},
        "service":       {"type": "keyword"},
        "kpi":           {"type": "keyword"},
        "value":         {"type": "float"},
        "unit":          {"type": "keyword"},
        "threshold":     {"type": "float"},
        "message":       {"type": "text"},
        "runbook_topic": {"type": "keyword"},
        "status":        {"type": "keyword"},
    }},
}

client = OpenSearch(OS_URL, timeout=30)
if "--recreate" in sys.argv and client.indices.exists(index=INDEX):
    client.indices.delete(index=INDEX)
if not client.indices.exists(index=INDEX):
    client.indices.create(index=INDEX, body=MAPPING); print(f"index {INDEX}: created")
else:
    print(f"index {INDEX}: exists")

alerts = json.load(io.open(SRC, encoding="utf-8"))
actions = ({"_index": INDEX, "_id": a["alert_id"], "_source": {**a, "@timestamp": a["timestamp"]}} for a in alerts)
ok, errors = helpers.bulk(client, actions, refresh=True, stats_only=False, raise_on_error=False)
print(f"loaded {ok} alerts, errors={len(errors)}")
r = client.search(index=INDEX, body={"size": 0, "aggs": {"sev": {"terms": {"field": "severity"}}, "hosts": {"cardinality": {"field": "host"}}}})
print("by severity:", [(b["key"], b["doc_count"]) for b in r["aggregations"]["sev"]["buckets"]], " hosts:", r["aggregations"]["hosts"]["value"])
