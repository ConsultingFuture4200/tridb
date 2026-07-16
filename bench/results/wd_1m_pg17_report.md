# TriDB Wikidata h2h — fused filter-first vs multi-store (matched)

> COMPUTE-BOUND regime (RAM-resident); value = fusion speed + one-WAL consistency. Latency/pages reported ONLY at matched recall. Seedless/vector-first mode blocked on 043. TriDB side = ONE fused statement (native typed BFS -> relational filter -> exact vector rank; `graph_store.assume_dense_open=on`, disclosed) — tjs_open's typed-traversal integration is the plan 038 residual, not part of this claim.

**Matched recall** (target 0.90): TriDB fusedh2 at recall 0.992, baseline h2f1024 at recall 0.986:

- TriDB fused filter-first statement: 0.14 ms
- multi-store (Milvus+Neo4j+pg): 3.34 ms
- **speedup: 23.68×**
