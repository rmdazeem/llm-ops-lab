# Eval run 20260918T112510Z
prompt=v2  judge=j2  model=qwen2.5:7b  corpus=sample_corpus  n=4

| metric | value |
|---|---|
| correctness (0-2) | 1.50 |
| faithfulness (0-2) | 2.00 |
| citation accuracy | 100% |
| refusal correctness | 100% |
| schema valid | 100% |
| **hallucination rate** | **0%** |

| id | corr | faith | cite | refusal | schema | latency | judge reason |
|---|---|---|---|---|---|---|---|
| q01 | 1 | 2 | Y | Y | Y | 295s | The assistant identifies the cause as a failing Consul service check, which is part of the |
| q03 | 2 | 2 | Y | Y | Y | 249s | The assistant identifies the same underlying cause as the expected root cause and all comm |
| q04 | 1 | 2 | Y | Y | Y | 250s | The cause is identified as the TLS certificate expiring, but the specific components (HAPr |
| q05 | 2 | 2 | Y | Y | Y | 206s | The assistant identifies the same underlying cause as the expected root cause, mentioning  |
