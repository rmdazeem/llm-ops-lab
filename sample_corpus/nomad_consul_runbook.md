# Runbook — Nomad + Consul (service scheduling and config)

All platform services (api-service, data-receiver, otel-collector, notification-processor, event-graph-generator,
rabbitmq, redis, haproxy, opensearch-*, percona) run as Docker tasks scheduled by Nomad. Consul provides service
discovery and the KV store that Nomad templates read at render time.

## Status

```bash
nomad job status                          # every job and its allocation counts
nomad job status <job>                    # allocations per node, versions, deployment state
nomad alloc status <alloc-id>             # task events: OOM, restart reason, exit code
nomad alloc logs -stderr <alloc-id>       # task stderr
consul members                            # cluster membership
consul catalog services                   # registered services
```

## Allocation restarting / dead

| Task event | Meaning | Fix |
|---|---|---|
| `OOM Killed` | container exceeded `resources { memory }` | raise memory in the jobspec or fix the leak; check `docker stats` |
| `Restart Signaled` + template change | Consul KV value changed with `change_mode = "restart"` | expected — verify the new KV value |
| `Failed to pull image` | registry unreachable or wrong tag | `docker pull <image>` on the node, check `insecure-registries` in daemon.json |
| exit code 137 | killed by kernel/OOM | same as OOM |
| exit code 1 immediately after start | config error | `nomad alloc logs -stderr`, then check the rendered file under `alloc/<id>/<task>/local/` |
| `dead` with `pending` siblings | dependency (OpenSearch/Redis) down | fix the dependency first, then `nomad job run <job>.nomad` |

Restart one allocation, not the whole job: `nomad alloc restart <alloc-id>`.

## Config lives in Consul KV, not files

Templates in the jobspec use `keyOrDefault "service/<name>/<key>"`. Editing `conf-template/` on disk is wrong —
upgrades overwrite it and the KV value wins anyway.

```bash
consul kv get -recurse service/otelcollector/     # read
consul kv put service/otelcollector/log_level debug   # write → task auto-restarts (change_mode=restart)
```

## Rendered file permissions

Nomad renders template files at the `perms` declared in the stanza (default `0644`) on **every** restart.
A manual `chmod` under `alloc/` is cosmetic. Durable fix: `perms = "0640"` in the jobspec, then `nomad job run`.

## Consul service check failing

1. `consul catalog nodes -service=<svc>` → which node registered it.
2. Compare the check (HTTP path/port) with what the container actually exposes: `docker port <id>`.
3. Host-network tasks: the port must be free on the host — `ss -ltnp | grep <port>`.
