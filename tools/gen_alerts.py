# -*- coding: utf-8 -*-
"""Generate synthetic AIOps-platform alerts/events -> data/alerts.json (no real hosts, IPs or bank names)."""
import json, random, io
from datetime import datetime, timedelta
from pathlib import Path

random.seed(42)
OUT = Path(__file__).resolve().parents[1] / "data" / "alerts.json"   # <repo>/data/alerts.json

HOSTS = {"host-01": "web", "host-02": "web", "host-03": "app", "host-04": "app", "host-05": "app",
         "host-06": "db", "host-07": "db", "host-08": "db"}
SERVICES = {
    "web": ["haproxy", "ui-service", "keycloak"],
    "app": ["api-service", "data-receiver", "otel-collector", "event-graph-generator",
            "notification-processor", "rabbitmq", "redis"],
    "db":  ["opensearch-master", "opensearch-txn", "opensearch-kpi", "percona-mysql", "trinodb"],
}
# (alert name, severity, kpi, unit, typical value range, related runbook topic)
TEMPLATES = [
    ("Nomad allocation restarting",        "critical", "restart_count",      "count", (3, 12),    "nomad"),
    ("Container OOMKilled",                "critical", "memory_used_pct",    "%",     (95, 100),  "nomad"),
    ("OpenSearch cluster status YELLOW",   "warning",  "unassigned_shards",  "count", (5, 141),   "opensearch"),
    ("OpenSearch cluster status RED",      "critical", "unassigned_shards",  "count", (150, 400), "opensearch"),
    ("JVM heap usage high",                "warning",  "heap_used_pct",      "%",     (85, 97),   "opensearch"),
    ("Disk usage high on /data",           "warning",  "disk_used_pct",      "%",     (85, 96),   "opensearch"),
    ("Percona replication lag",            "warning",  "seconds_behind",     "s",     (30, 900),  "percona"),
    ("Percona VIP failover",               "critical", "failover_events",    "count", (1, 2),     "percona"),
    ("HAProxy backend DOWN",               "critical", "backends_down",      "count", (1, 3),     "haproxy"),
    ("HAProxy 5xx rate high",              "warning",  "http_5xx_rate",      "%",     (2, 15),    "haproxy"),
    ("TLS certificate expiring",           "warning",  "days_to_expiry",     "days",  (3, 29),    "certificate"),
    ("Keycloak login failures",            "warning",  "login_failures_5m",  "count", (20, 200),  "certificate"),
    ("Consul service check failing",       "warning",  "failing_checks",     "count", (1, 4),     "consul"),
    ("OTel collector export errors",       "warning",  "export_errors_5m",   "count", (10, 500),  "otel"),
    ("Data receiver queue backlog",        "warning",  "queue_depth",        "count", (5000, 90000), "rabbitmq"),
    ("Redis connection refused",           "critical", "conn_errors_5m",     "count", (5, 60),    "redis"),
    ("Transaction latency P95 high",       "warning",  "txn_p95_ms",         "ms",    (1200, 8000), "opensearch"),
    ("NFS mount unreachable",              "critical", "nfs_rpc_timeouts",   "count", (1, 10),    "nfs"),
]
SVC_FOR_TOPIC = {
    "nomad": None, "opensearch": ["opensearch-master", "opensearch-txn", "opensearch-kpi"],
    "percona": ["percona-mysql"], "haproxy": ["haproxy"], "certificate": ["haproxy", "keycloak", "ui-service"],
    "consul": None, "otel": ["otel-collector"], "rabbitmq": ["data-receiver", "rabbitmq"], "redis": ["redis", "api-service"],
    "nfs": ["api-service", "data-receiver"],
}

def pick_host_service(topic):
    svcs = SVC_FOR_TOPIC[topic]
    if svcs:
        svc = random.choice(svcs)
        role = next(r for r, lst in SERVICES.items() if svc in lst)
    else:
        role = random.choice(list(SERVICES))
        svc = random.choice(SERVICES[role])
    host = random.choice([h for h, r in HOSTS.items() if r == role])
    return host, svc

def main():
    t0 = datetime(2026, 9, 10, 0, 0, 0)
    alerts = []
    for i in range(300):
        name, sev, kpi, unit, (lo, hi), topic = random.choice(TEMPLATES)
        host, svc = pick_host_service(topic)
        ts = t0 + timedelta(minutes=random.randint(0, 7 * 24 * 60))
        val = round(random.uniform(lo, hi), 1) if unit in ("%", "ms", "s") else random.randint(lo, hi)
        alerts.append({
            "alert_id": f"ALT-{100000 + i}",
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "severity": sev,
            "alert_name": name,
            "host": host,
            "role": HOSTS[host],
            "service": svc,
            "kpi": kpi,
            "value": val,
            "unit": unit,
            "threshold": lo,
            "message": f"{name} on {host} ({svc}): {kpi}={val}{unit} (threshold {lo}{unit})",
            "runbook_topic": topic,
            "status": random.choice(["open", "open", "acknowledged", "resolved"]),
        })
    alerts.sort(key=lambda a: a["timestamp"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    io.open(OUT, "w", encoding="utf-8").write(json.dumps(alerts, indent=1))
    print(f"wrote {len(alerts)} alerts -> {OUT}")
    from collections import Counter
    for k, v in Counter(a["alert_name"] for a in alerts).most_common(5):
        print(f"  {v:3d}  {k}")

if __name__ == "__main__":
    main()
