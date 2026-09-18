# -*- coding: utf-8 -*-
"""Compare retrieval from the in-memory index vs the OpenSearch k-NN index for the same questions (no LLM call)."""
import os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "project1-rca-assistant"))
os.environ.setdefault("RCA_TRACE_EXPORTER", "none")
import rca_assistant as rca

QS = sys.argv[1:] or ["HAProxy backend is DOWN on host-01, how do I troubleshoot?", "why is the Windows Power BI server slow?"]

def show(label, index):
    for q in QS:
        t = time.time()
        nodes = index.as_retriever(similarity_top_k=3).retrieve(q)
        print(f"  [{label}] {q[:45]:45s} {time.time()-t:4.1f}s  " +
              "  ".join(f"{n.metadata.get('file_name')}={n.score:.3f}" for n in nodes))

print("== OpenSearch k-NN ==");  rca.VECTOR_STORE = "opensearch"; show("opensearch", rca.build_or_load_index())
print("== local in-memory ==");  rca.VECTOR_STORE = "local";      show("local", rca.build_or_load_index())
