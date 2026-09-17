# Runbook — HAProxy (containerised, Nomad-scheduled)

Topology: two web nodes (host-01, host-02) run HAProxy as a Docker container scheduled by Nomad. A Pacemaker
VIP (10.0.0.11) floats between them. Backends are the app nodes (host-03..host-05) on ports 9998 (UI/API) and 14250 (OTLP gRPC).

## Symptoms and first checks

| Symptom | First check |
|---|---|
| UI unreachable on VIP | `ip addr show \| grep 10.0.0.11` on both web nodes — exactly one must hold the VIP |
| Backend DOWN in stats | `curl -s http://127.0.0.1:14567/stats` on the web node; look for `DOWN` rows |
| 5xx rate high | check backend health (app node) before touching HAProxy |

## Container status and logs

```bash
docker ps --filter name=haproxy                      # is the container Up?
docker logs --tail 100 <container-id>                # startup / health-check events
nomad job status haproxy                             # allocation state per node
```

## Validate configuration before reload

Config lives on the host at `/opt/lb/conf/haproxy.cfg` and is bind-mounted into the container at
`/usr/local/etc/haproxy/haproxy.cfg` — every path *inside* the config must be a container path.

```bash
docker exec <container-id> haproxy -c -f /usr/local/etc/haproxy/haproxy.cfg
```
Only the final line `Configuration file is valid` matters; timeout warnings on backends are pre-existing noise.

## Reload without dropping connections

HAProxy runs in master-worker mode, so a HUP is a graceful reload:
```bash
docker kill -s HUP <container-id>
```
Never `nomad job restart haproxy` on a two-node setup — it can bounce both allocations and drop the VIP.
Use `nomad alloc restart <alloc-id>` one node at a time.

## Backend DOWN — root causes seen

1. App-node service not listening: `ss -ltnp | grep 9998` on the app node.
2. Health-check path changed after an upgrade: compare `option httpchk` in haproxy.cfg with the service's health endpoint.
3. TLS handshake failure to backend after cert rotation: `openssl s_client -connect host-03:9998 </dev/null | grep Verify`.
4. Firewall rule removed between web and app subnets: `curl -sk --connect-timeout 5 https://host-03:9998 -o /dev/null -w "%{http_code}"`.

## Non-breaking-space trap
Config pasted from a browser may contain U+00A0. HAProxy reports `unknown keyword '   '`.
Find: `grep -nP '[^\x00-\x7F]' haproxy.cfg`   Fix: `sed -i 's/\xc2\xa0/ /g' haproxy.cfg`
