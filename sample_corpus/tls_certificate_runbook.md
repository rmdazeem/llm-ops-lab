# Runbook — TLS certificates, Keycloak SSO and the OpenTelemetry pipeline

## Certificate expiring (< 30 days)

Where the certificate is used — all four must be updated, in this order:

| Component | Location | Format |
|---|---|---|
| HAProxy | `/opt/lb/certs/site.pem` (key + cert + chain in one file) | PEM |
| Keycloak | keystore `/opt/sso/conf/server.keystore` | PKCS12/JKS |
| ui-service JVM truststore | `/opt/app/data/cert/cacerts` | JKS — **import the CA chain here too** |
| OTLP ingest (HAProxy :14250) | same HAProxy PEM | PEM |

```bash
openssl x509 -in site.crt -noout -dates -subject -ext subjectAltName
openssl verify -CAfile chain.pem site.crt                       # must print OK
cat site.key site.crt intermediate.crt root.crt > site.pem       # HAProxy order: key, leaf, intermediates, root
keytool -importcert -alias ca-root -file root.crt -keystore cacerts -storepass changeit -noprompt
```

Then graceful reload: `docker kill -s HUP <haproxy-id>`; Keycloak needs a container restart.

## Blank UI after certificate change (PKIX error)

Symptom: browser login to Keycloak succeeds, the UI loads assets with HTTP 200 but renders blank.
`ui-service.log` shows `PKIX path building failed ... unable to find valid certification path`.

Cause: ui-service validates the login token by calling Keycloak over TLS and does not trust the new CA.
Fix: import root + intermediate into the JVM truststore (`cacerts`) used by ui-service, restart ui-service.
This step is commonly missing from vendor guides — check it first.

## Keycloak login failures

1. `docker logs --tail 200 <keycloak-id> | grep -i "login_error\|ldap"`.
2. LDAP federation: test bind with `ldapsearch -x -H ldap://<ad-host>:389 -D "<bind-dn>" -W -b "<base-dn>" "(sAMAccountName=<user>)"`.
3. Users logged out mid-session: check HAProxy Host-header allow-list ACLs — an internal hostname missing from the list drops the session.
4. Cookies flagged by an audit (`SameSite=None`) are Keycloak's by design for the login-status iframe; do not rewrite them at the proxy.

## OpenTelemetry collector — export errors

Pipeline: Java agent → HAProxy :14250 → otel-collector (Jaeger gRPC :14252, OTLP :4317) → OpenSearch exporter → OpenSearch :9250.

```bash
curl -s http://host-03:13133/            # collector health
curl -s http://host-03:8888/metrics | grep -E "exporter_send_failed|receiver_refused"
docker logs --tail 100 <otel-collector-id> | grep -iE "error|refused|tls"
```
- `send_failed` rising + OpenSearch reachable → check the exporter's TLS: `insecure_skip_verify` vs the certificate SANs (IP SANs required for IP endpoints).
- Counter KPIs rejected as `Snapshot`: metrics typed as cumulative must be declared Delta on the exporter side.
- Config changes go through Consul KV (see the Nomad/Consul runbook) — the collector restarts itself.
