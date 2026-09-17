# Runbook — Percona MySQL (2-node HA) and Redis

## Percona MySQL

Two DB nodes (host-06, host-07) run Percona 8.0 in containers with a floating VIP (10.0.0.9:3307). Applications must
connect to the VIP, never to a node IP — a service pointed at a node IP silently breaks on failover.

### Replication lag

```bash
docker exec <percona-id> mysql -uroot -e "SHOW REPLICA STATUS\G" | grep -E "Running|Seconds_Behind|Last_.*Error"
```
- `Seconds_Behind_Source` growing: long transactions on the source, or the replica's disk is slow — check `iostat -x 5`.
- `Replica_SQL_Running: No` with a duplicate-key error: a write went to the replica directly. Find the client, repoint it to the VIP.
- Heavy batch deletes: chunk them (`LIMIT 5000` in a loop) so the replica keeps up.

### VIP failover happened

1. `pcs status` — which node holds the VIP resource now, any failed actions.
2. `pcs resource cleanup` after fixing the cause.
3. Check application logs for `Communications link failure` around the failover time; connection pools recover on their own if they use the VIP.

### Password and TLS policy changes (audit findings)

- `default_password_lifetime` is evaluated retroactively against `password_last_changed`. Before setting 365, run
  `ALTER USER '<svc>'@'%' PASSWORD EXPIRE NEVER` for every service account or they expire on next connect.
- `SET PERSIST` writes `mysqld-auto.cnf` into the datadir and survives container restarts.
- Reverting cipher settings: use `SET GLOBAL tls_ciphersuites=NULL`, never `= ''` (empty string means *no ciphers* and the next
  `ALTER INSTANCE RELOAD TLS` fails with ERROR 3888).

## Redis

Six Redis instances on the app nodes form a cluster with Sentinel on 26379.

```bash
redis-cli -h host-03 -p 6379 ping
redis-cli -h host-03 -p 6379 cluster info | grep cluster_state
redis-cli -h host-03 -p 6379 info memory | grep used_memory_human
redis-cli -h host-03 -p 26379 sentinel masters
```

### Connection refused from api-service

1. Is the container up? `docker ps | grep redis`; if restarting, `docker logs --tail 50 <id>` (usually `maxmemory` reached or AOF corruption).
2. `cluster_state:fail` → a master and all its replicas are down; bring the node back, then `redis-cli --cluster fix host-03:6379`.
3. Service still failing after Redis is healthy → it cached the old master IP; `nomad alloc restart <api-service-alloc>`.

### Memory

`maxmemory-policy allkeys-lru` for cache workloads. If `used_memory` ≈ `maxmemory` and policy is `noeviction`, writes fail with OOM.
