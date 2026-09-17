# Runbook — OpenSearch cluster (2.x, Docker, Nomad-scheduled)

Topology: three DB nodes (host-06..host-08). Each runs several OpenSearch instances (master, txn, kpi, coordinator)
as containers. Data on `/data/es{1..4}`. Admin user `admin`, TLS on 9200. Config is rendered by Nomad templates
from `/opt/search/config/*.yml.tpl` — edit the template, not the rendered file under `alloc/`.

## Health

```bash
curl -sk -u admin https://host-06:9200/_cluster/health?pretty
curl -sk -u admin https://host-06:9200/_cat/nodes?v
curl -sk -u admin https://host-06:9200/_cat/indices?v&health=red
curl -sk -u admin "https://host-06:9200/_cluster/allocation/explain?pretty"     # WHY a shard is unassigned
```

| Status | Meaning | Action |
|---|---|---|
| GREEN | all primaries + replicas allocated | none |
| YELLOW | replicas unassigned | tolerable short-term; check disk and node count |
| RED | at least one PRIMARY unassigned | data unavailable for that index — act now |

## RED cluster — order of checks

1. Is a node missing? `_cat/nodes` vs expected count. A dead container: `docker ps -a | grep opensearch`, `docker logs --tail 200 <id>`.
2. Disk watermark: `_cat/allocation?v`. Above 85% OpenSearch stops allocating; above 95% it marks indices read-only.
3. JVM heap: `_nodes/stats/jvm` — heap > 90% sustained → GC storms → node drops out. Heap must be ≤ 50% of RAM and ≤ 31 GB.
4. `allocation/explain` gives the exact reason (no valid node, disk, awareness attribute, corrupt shard).
5. Corrupt shard with no replica: restore from snapshot; last resort `_cluster/reroute` with `allocate_empty_primary` (data loss).

## JVM heap high

- Check `jvm.options` / `OPENSEARCH_JAVA_OPTS` for `-Xms/-Xmx`; JDK 17 rejects CMS flags (`UseConcMarkSweepGC`) — remove them.
- Reduce shard count: too many small shards is the usual cause. Target 10–50 GB per shard.

## Disk usage high on data volume

- ISM policy: hot 3 days → warm 10 days → delete. Check `_plugins/_ism/explain/<index>`.
- Force-merge old read-only indices; delete `*_backup` copies.
- Never delete files under `/data/es*` by hand — remove indices via the API.

## Shard placement on few physical hosts

If several instances share one physical box, enable rack awareness so primary and replica never land on the same host:
`cluster.routing.allocation.awareness.attributes: rack` (persistent setting) and set `node.attr.rack` per instance.

## Remote reindex from an old cluster

- Add the source to `reindex.remote.whitelist` on the destination (needs restart).
- Set `index.default_pipeline: _none` on the destination index first if the destination has no ingest nodes.
- `_refresh` before comparing document counts.
