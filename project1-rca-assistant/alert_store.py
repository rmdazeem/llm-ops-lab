# -*- coding: utf-8 -*-
"""
Project 3a — alerts from OpenSearch instead of a keyword scan over a JSON file.

search_alerts(question) builds one query from the question:
  - host / service names are extracted and NORMALISED (host01, host 01, HOST-01 -> host-01) and used as exact filters (boost)
  - the remaining words go to a multi_match over message / alert_name with fuzziness (typos tolerated)
  - only open/acknowledged alerts inside the time window, newest first
Falls back to the old JSON keyword scan if OpenSearch is unreachable, so the assistant never dies with the index.
"""
import os, re, json, io
from pathlib import Path
from opensearchpy import OpenSearch, ConnectionError as OSConnError

OS_URL = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")
INDEX = "alerts"
ALERTS_JSON = Path(__file__).resolve().parents[1] / "data" / "alerts.json"
KNOWN_SERVICES = {"haproxy", "ui-service", "keycloak", "api-service", "data-receiver", "otel-collector",
                  "event-graph-generator", "notification-processor", "rabbitmq", "redis",
                  "opensearch-master", "opensearch-txn", "opensearch-kpi", "percona-mysql", "trinodb", "opensearch", "percona"}
HOST_RE = re.compile(r"\bhost[\s_-]?0?(\d{1,2})\b", re.I)

_client = None
def client():
    global _client
    if _client is None:
        _client = OpenSearch(OS_URL, timeout=10)
    return _client


def parse(question: str) -> dict:
    hosts = sorted({f"host-{int(m):02d}" for m in HOST_RE.findall(question)})
    q = question.lower()
    services = sorted({s for s in KNOWN_SERVICES if s in q})
    if "opensearch" in services: services += ["opensearch-master", "opensearch-txn", "opensearch-kpi"]
    if "percona" in services: services += ["percona-mysql"]
    text = HOST_RE.sub(" ", question)
    for s in services: text = text.replace(s, " ")
    text = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    words = [w for w in text.split() if len(w) > 3 and w not in {"what", "why", "how", "does", "should", "check", "first", "this", "that", "with", "from", "there"}]
    return {"hosts": hosts, "services": sorted(set(services)), "text": " ".join(words)}


def search_alerts(question: str, window: str = "now-30d", limit: int = 8) -> tuple[list, dict]:
    p = parse(question)
    must, should = [{"range": {"@timestamp": {"gte": window}}}], []
    if p["hosts"]:    must.append({"terms": {"host": p["hosts"]}})
    if p["services"]: should.append({"terms": {"service": p["services"], "boost": 3}})
    if p["text"]:     should.append({"multi_match": {"query": p["text"], "fields": ["message^2", "alert_name^3"], "fuzziness": "AUTO"}})
    body = {"size": limit, "query": {"bool": {"must": must, "should": should, "minimum_should_match": 1 if should else 0,
                                              "must_not": [{"term": {"status": "resolved"}}]}},
            "sort": [{"_score": "desc"}, {"@timestamp": "desc"}]}
    try:
        r = client().search(index=INDEX, body=body)
        hits = [h["_source"] for h in r["hits"]["hits"]]
        return hits, {"source": "opensearch", "parsed": p, "total": r["hits"]["total"]["value"]}
    except OSConnError:
        return _fallback(question, limit), {"source": "json-fallback", "parsed": p, "total": -1}


def _fallback(question, limit):
    alerts = json.load(io.open(ALERTS_JSON, encoding="utf-8"))
    words = {w for w in re.findall(r"[a-z0-9\-]+", question.lower()) if len(w) > 2}
    score = lambda a: sum(1 for w in words if w in " ".join(str(v) for v in a.values()).lower())
    return [a for a in sorted(alerts, key=lambda a: (score(a), a["timestamp"]), reverse=True)[:limit] if score(a) > 0]


if __name__ == "__main__":
    import sys
    for q in (sys.argv[1:] or ["why is api-service restarting on host-03?", "what is cpu uilz on host01 severs", "HAProxy backend is DOWN on host-01"]):
        hits, meta = search_alerts(q)
        print(f"\n{q}\n  parsed={meta['parsed']}  source={meta['source']} total={meta['total']}")
        for h in hits[:5]: print(f"   - {h['timestamp']} {h['severity']:8s} {h['message']}")
