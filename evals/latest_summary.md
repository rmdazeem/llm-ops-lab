# Eval run 20260918T131811Z
prompt=v3  judge=j2  model=qwen2.5:7b  corpus=sample_corpus  n=3

| metric | value |
|---|---|
| correctness (0-2) | 2.00 |
| faithfulness (0-2) | 2.00 |
| citation accuracy | 100% |
| refusal correctness | 100% |
| schema valid | 100% |
| **hallucination rate** | **0%** |

| id | corr | faith | cite | refusal | schema | latency | judge reason |
|---|---|---|---|---|---|---|---|
| q03 | 2 | 2 | Y | Y | Y | 504s | The assistant identifies the correct cause (app-node service not listening) and the eviden |
| t01 | 2 | 2 | Y | Y | Y | 22s | trap refused correctly |
| t02 | 2 | 2 | Y | Y | Y | 0s | trap refused correctly |
