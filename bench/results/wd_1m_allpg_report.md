# All-Postgres baseline vs fused tjs statement — 1M Wikidata (stock PG17)

> Both contenders in the SAME database/container (`tridb-wikidata-pg17`), same
> 50 pinned oracle queries as Gate B, client-clocked psycopg over TCP, warm,
> median of 25 runs/query. Full method + honesty notes:
> `docs/benchmark_allpg_baseline_v0.1.0.md`.

| Contender | recall@10 | median | p95 |
|---|---:|---:|---:|
| A — fused (native BFS + P31 + pgvector rank) | 0.986 | 0.049 ms | 0.310 ms |
| B — all-PG SQL (recursive CTE over `links`) | 0.986 | 0.065 ms | 0.353 ms |

- B / A median latency ratio: **1.33×** (paired: A faster on 48/50 queries, median B-A = 15.5 us)
- server-side (EXPLAIN ANALYZE) exec median: A 0.034 ms vs B 0.052 ms; planning A 0.021 ms vs B 0.051 ms
- reach sets equal on all queries: **True**; returned ids identical: **True**
- C — multi-store (Milvus+Neo4j+pg): 3.34 ms at recall 0.986 (cited from Gate B, not re-run)

## Seedless (filtered-ANN) leg

| Point | recall@10 | median | p95 |
|---|---:|---:|---:|
| tjs_open tc=16 budget=80000 | 0.822 | 0.967 ms | 69.844 ms |
| tjs_open tc=64 budget=20000 | 0.832 | 1.705 ms | 119.310 ms |
| tjs_open tc=256 budget=20000 | 0.842 | 12.264 ms | 219.713 ms |
| pgvector ef_search=40 | 0.806 | 0.712 ms | 25.548 ms |
| pgvector ef_search=100 | 0.820 | 0.762 ms | 26.509 ms |
| pgvector ef_search=200 | 0.834 | 1.395 ms | 28.124 ms |
| pgvector ef_search=400 | 0.858 | 2.607 ms | 30.282 ms |
| pgvector ef_search=800 | 0.872 | 5.103 ms | 29.399 ms |

