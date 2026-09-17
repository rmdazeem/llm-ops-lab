# Eval log — Project 1 (manual grading, 17 Sep 2026)

Scoring: citations real? | alerts real? | commands from runbook? | confidence honest? | verdict /10

## Q1: why is api-service restarting on host-03?
- citations real: yes (2 files)   alerts real: yes (NFS timeouts + Redis refused on host-03)
- commands from runbook: yes (nomad alloc logs / alloc restart, redis-cli checks)
- confidence: medium — reasonable
- verdict: 8/10 — correct correlation of two alerts; root-cause sentence is a bit generic
- latency: 221–255 s on CPU (qwen2.5:7b, ~1.5k-token prompt)

## Q3: HAProxy backend is DOWN on host-01, how do I troubleshoot?
- citations real: yes, but the same file listed twice (dedupe needed)
- commands from runbook: yes, verbatim
- BUT the runbook itself was stale (systemd/journalctl commands for a service that runs as a container) and the
  model reproduced it confidently.
- verdict: model 9/10, corpus 4/10 — RAG quality = corpus quality. Fixed the runbook, re-indexed.
- ACTION: keep corpus review in the loop; evals must flag "correct per runbook but wrong for the environment"

## Q-cpu: what is cpu uilz on host01 severs  (typo on purpose)
- 0 alerts matched (no CPU alert type in data; keyword matcher needs host-01 not host01)
- answer: "Insufficient evidence", confidence low, no citations — correct behaviour
- ACTION (Project 3): replace keyword match with a real query tool; normalise host names

## Q4 (trap): why is the Windows Power BI server slow?
- retrieval still returned 3 unrelated chunks (vector search always returns nearest neighbours)
- answer: "Insufficient evidence", confidence low, no citations — trap passed
- ACTION (Project 2): similarity-score threshold before chunks reach the prompt; count refusals as a metric

## Summary
- 4 questions: 2 correct, 1 correct-refusal, 1 correct-per-runbook-but-runbook-wrong
- Hallucinated commands: 0    Invented citations: 0    Refusals: 2/2 correct
- Biggest risk found: stale runbooks, not the model
