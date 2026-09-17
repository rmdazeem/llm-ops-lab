# -*- coding: utf-8 -*-
"""
Copy internal runbooks into corpus/ with sensitive values replaced by stable pseudonyms.
- IPv4 addresses      -> 10.0.<n>.<m>   (same real IP always maps to the same fake IP)
- Bank hostnames      -> host-NN
- Bank names/domains  -> BankX / bankx.example
- Passwords/secrets   -> REDACTED
Writes corpus/REDACTION_MAP.json (kept OUT of git) so you can trace a fake value back if needed.
"""
import re, json, sys, io
from pathlib import Path

# patterns + paths live in tools/redact_patterns.local.json (gitignored) so the script itself is generic
_CFG = json.load(io.open(Path(__file__).with_name("redact_patterns.local.json"), encoding="utf-8"))
SRC = Path(_CFG["src"])
DST = Path(_CFG["dst"])
INCLUDE_EXT = {".md", ".sh"}                 # .py config skipped on purpose (raw creds)
FIXED = [(pat, rep) for pat, rep in _CFG["fixed"]]
# lines like  password: xxx / passwd=xxx / -p xxx / PASSWORD "xxx"
SECRET_LINE = re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)(\s*[:=]\s*|\s+)([\"']?)([^\s\"']+)(\3)")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
HOST = re.compile(r"\b(?:dc|dr)svr[a-z0-9]+\b", re.I)

ip_map, host_map = {}, {}

def fake_ip(real):
    if real not in ip_map:
        n = len(ip_map) + 1
        ip_map[real] = f"10.0.{n // 250}.{n % 250 + 1}"
    return ip_map[real]

def fake_host(real):
    key = real.lower()
    if key not in host_map:
        host_map[key] = f"host-{len(host_map) + 1:02d}"
    return host_map[key]

def redact(text):
    for pat, rep in FIXED:
        text = re.sub(pat, rep, text, flags=re.I)
    text = IPV4.sub(lambda m: fake_ip(m.group(0)), text)
    text = HOST.sub(lambda m: fake_host(m.group(0)), text)
    text = SECRET_LINE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}REDACTED{m.group(5)}", text)
    return text

def main():
    files = [p for p in SRC.rglob("*") if p.is_file() and p.suffix.lower() in INCLUDE_EXT]
    DST.mkdir(parents=True, exist_ok=True)
    for p in files:
        out = DST / Path(redact(str(p.relative_to(SRC))))   # redact file names too
        out.parent.mkdir(parents=True, exist_ok=True)
        raw = io.open(p, encoding="utf-8", errors="replace").read()
        io.open(out, "w", encoding="utf-8").write(redact(raw))
    io.open(DST / "REDACTION_MAP.json", "w", encoding="utf-8").write(
        json.dumps({"ips": ip_map, "hosts": host_map}, indent=2))
    print(f"copied {len(files)} files -> {DST}")
    print(f"ips replaced: {len(ip_map)}   hostnames replaced: {len(host_map)}")

if __name__ == "__main__":
    main()
