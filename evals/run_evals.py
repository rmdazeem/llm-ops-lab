# -*- coding: utf-8 -*-
"""
Eval harness: ask every golden-set question, score the answer, store results.

  python evals/run_evals.py                 # full golden set
  python evals/run_evals.py q01 t01         # subset by id

Scores per question
  citation_ok   deterministic  every expected file is in the model's citations (traps: no citations)
  refusal_ok    deterministic  traps must answer with confidence=low / "insufficient"; real questions must NOT refuse
  schema_ok     deterministic  valid JSON with all required keys
  correctness   LLM judge 0-2  does the root cause match the expected one?
  faithfulness  LLM judge 0-2  is everything in the answer supported by the retrieved runbooks + alerts?
  hallucinated  derived        faithfulness == 0 or (refusal expected but answer invented something)

Storage
  evals/results.sqlite   tables runs, results  (SQL you can query with any Postgres-style client)
  OpenSearch llm-evals   one doc per result -> Grafana "hallucination rate" panel
  evals/latest_summary.md
"""
import json, io, os, re, sys, time, sqlite3, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "project1-rca-assistant"))
import rca_assistant as rca                                   # noqa: E402  (loads models + tracing)
from llama_index.core import Settings                          # noqa: E402

GOLDEN = ROOT / "evals" / "golden_set.json"
DB     = ROOT / "evals" / "results.sqlite"
SUMMARY = ROOT / "evals" / "latest_summary.md"
OS_URL = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")
REQUIRED_KEYS = {"root_cause", "evidence", "next_steps", "citations", "confidence"}

JUDGE_PROMPT = """You are grading an SRE assistant's answer. Be strict and literal.

### QUESTION
{question}

### EXPECTED ROOT CAUSE (written by a senior SRE)
{expected}

### CONTEXT THE ASSISTANT WAS GIVEN (runbook excerpts + alerts)
{context}

### ASSISTANT'S ANSWER (JSON)
{answer}

Score two things. Judge MEANING, not wording.
1. correctness — does the assistant identify the same underlying cause as the EXPECTED ROOT CAUSE?
   2 = same cause, even if phrased differently or shorter (e.g. "NFS and Redis issues" == "NFS unreachable and Redis connection refused").
   1 = only part of the cause, or a generic statement that merely restates the alert/question.
   0 = a different cause, or no cause given.
   Missing next_steps do NOT reduce correctness below 1 if the cause itself is right.
2. faithfulness — is anything in the answer NOT supported by the CONTEXT?
   2 = every host, command, file and claim appears in the CONTEXT (a short answer with few details is still 2).
   1 = one detail is not in the CONTEXT (e.g. a command path or host that was not given).
   0 = invents a cause, command, host or file that is not in the CONTEXT at all.
   Lack of detail is NOT unfaithfulness. Only penalise things the assistant made up.
Return STRICT JSON only: {{"correctness": 0|1|2, "faithfulness": 0|1|2, "reason": "one sentence naming the specific match or the specific invented item"}}"""
JUDGE_VERSION = "j2"


def http(method, path, body=None):
    data = None; headers = {}
    if body is not None:
        data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/x-ndjson" if isinstance(body, str) else "application/json"
    req = urllib.request.Request(OS_URL + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except Exception as e:                                    # OpenSearch down -> evals still run
        return 0, {"error": str(e)}


def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS runs (
      run_id TEXT PRIMARY KEY, ts TEXT, prompt_version TEXT, judge_version TEXT, model TEXT, corpus TEXT, n INTEGER,
      avg_correctness REAL, avg_faithfulness REAL, citation_rate REAL, refusal_rate REAL, schema_rate REAL, hallucination_rate REAL);
    CREATE TABLE IF NOT EXISTS results (
      run_id TEXT, qid TEXT, question TEXT, trace_id TEXT, must_refuse INTEGER,
      answer_json TEXT, expected TEXT, retrieved_files TEXT,
      schema_ok INTEGER, citation_ok INTEGER, refusal_ok INTEGER, correctness INTEGER, faithfulness INTEGER, hallucinated INTEGER,
      judge_reason TEXT, latency_s REAL, judge_latency_s REAL);
    """)
    return con


def is_refusal(data):
    if not data: return False
    rc = (data.get("root_cause") or "").lower()
    return data.get("confidence") == "low" or "insufficient" in rc or "not enough" in rc or "cannot determine" in rc


def judge(question, expected, context, answer_json):
    prompt = JUDGE_PROMPT.format(question=question, expected=expected, context=context[:6000], answer=json.dumps(answer_json)[:3000])
    t = time.time()
    raw = Settings.llm.complete(prompt).text
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        j = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        j = {}
    return int(j.get("correctness", 0)), int(j.get("faithfulness", 0)), j.get("reason", "judge returned no JSON"), time.time() - t


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    golden = json.load(io.open(GOLDEN, encoding="utf-8"))
    only = set(sys.argv[1:])
    if only: golden = [g for g in golden if g["id"] in only]
    index = rca.build_or_load_index()
    con = init_db()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    model = Settings.llm.model
    print(f"[run {run_id}] prompt={rca.PROMPT_VERSION} judge={JUDGE_VERSION} model={model} corpus={rca.CORPUS.name} questions={len(golden)}")

    rows = []
    for g in golden:
        q = g["question"]
        t = time.time()
        data, raw, nodes, alerts, _ = rca.answer(index, q)
        latency = time.time() - t
        trace_id = rca.LAST_TRACE_ID
        files = [n.metadata.get("file_name") for n in nodes]
        context = "\n\n".join(f"[{n.metadata.get('file_name')}]\n{n.get_content()}" for n in nodes) + \
                  "\n\nALERTS:\n" + "\n".join(a["message"] for a in alerts)

        schema_ok = bool(data) and REQUIRED_KEYS <= set(data.keys())
        cited = set(data.get("citations", [])) if data else set()
        if g["must_refuse"]:
            citation_ok = True                              # nothing to cite; judged by refusal instead
            refusal_ok = is_refusal(data)
        else:
            citation_ok = set(g["expected_files"]) <= cited
            refusal_ok = not is_refusal(data)

        if g["must_refuse"] and refusal_ok:
            correctness, faithfulness, reason, jl = 2, 2, "trap refused correctly", 0.0
        else:
            correctness, faithfulness, reason, jl = judge(q, g["expected_root_cause"], context, data or {"raw": raw[:1500]})
        hallucinated = int(faithfulness == 0 or (g["must_refuse"] and not refusal_ok))

        rows.append((run_id, g["id"], q, trace_id, int(g["must_refuse"]), json.dumps(data), g["expected_root_cause"],
                     json.dumps(files), int(schema_ok), int(citation_ok), int(refusal_ok), correctness, faithfulness,
                     hallucinated, reason, round(latency, 1), round(jl, 1)))
        flag = "HALLUCINATION" if hallucinated else ""
        print(f"  {g['id']}  corr={correctness} faith={faithfulness} cite={'Y' if citation_ok else 'N'} "
              f"refusal={'Y' if refusal_ok else 'N'} schema={'Y' if schema_ok else 'N'}  {latency:.0f}s+{jl:.0f}s  {flag}")
        print(f"        judge: {reason[:140]}")

    n = len(rows)
    summ = dict(
        avg_correctness=sum(r[11] for r in rows) / n, avg_faithfulness=sum(r[12] for r in rows) / n,
        citation_rate=sum(r[9] for r in rows) / n, refusal_rate=sum(r[10] for r in rows) / n,
        schema_rate=sum(r[8] for r in rows) / n, hallucination_rate=sum(r[13] for r in rows) / n)
    con.executemany("INSERT INTO results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute("INSERT INTO runs (run_id, ts, prompt_version, judge_version, model, corpus, n, avg_correctness, avg_faithfulness, "
                "citation_rate, refusal_rate, schema_rate, hallucination_rate) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, datetime.now(timezone.utc).isoformat(), rca.PROMPT_VERSION, JUDGE_VERSION, model, rca.CORPUS.name, n,
                 summ["avg_correctness"], summ["avg_faithfulness"], summ["citation_rate"], summ["refusal_rate"],
                 summ["schema_rate"], summ["hallucination_rate"]))
    con.commit()

    # -> OpenSearch (for Grafana); silently skipped if it is down
    body = ""
    for r in rows:
        doc = {"@timestamp": int(time.time() * 1000), "run_id": run_id, "qid": r[1], "question": r[2], "trace_id": r[3],
               "must_refuse": int(r[4]), "schema_ok": int(r[8]), "citation_ok": int(r[9]), "refusal_ok": int(r[10]),   # 0/1 ints, not booleans:
               "correctness": r[11], "faithfulness": r[12], "hallucinated": int(r[13]), "latency_s": r[15],         # Grafana can Average them
               "prompt_version": rca.PROMPT_VERSION, "judge_version": JUDGE_VERSION, "model": model, "corpus": rca.CORPUS.name}
        body += json.dumps({"index": {"_index": "llm-evals", "_id": f"{run_id}-{r[1]}"}}) + "\n" + json.dumps(doc) + "\n"
    code, _ = http("POST", "/_bulk?refresh=true", body)

    md = [f"# Eval run {run_id}", f"prompt={rca.PROMPT_VERSION}  judge={JUDGE_VERSION}  model={model}  corpus={rca.CORPUS.name}  n={n}", "",
          "| metric | value |", "|---|---|",
          f"| correctness (0-2) | {summ['avg_correctness']:.2f} |", f"| faithfulness (0-2) | {summ['avg_faithfulness']:.2f} |",
          f"| citation accuracy | {summ['citation_rate']:.0%} |", f"| refusal correctness | {summ['refusal_rate']:.0%} |",
          f"| schema valid | {summ['schema_rate']:.0%} |", f"| **hallucination rate** | **{summ['hallucination_rate']:.0%}** |", "",
          "| id | corr | faith | cite | refusal | schema | latency | judge reason |", "|---|---|---|---|---|---|---|---|"]
    md += [f"| {r[1]} | {r[11]} | {r[12]} | {'Y' if r[9] else 'N'} | {'Y' if r[10] else 'N'} | {'Y' if r[8] else 'N'} | {r[15]:.0f}s | {r[14][:90]} |" for r in rows]
    io.open(SUMMARY, "w", encoding="utf-8").write("\n".join(md) + "\n")
    print("\n" + "\n".join(md[3:11]))
    print(f"\n[stored] sqlite={DB.name}  opensearch=llm-evals ({'ok' if code == 200 else 'skipped'})  summary={SUMMARY.name}")


if __name__ == "__main__":
    main()
