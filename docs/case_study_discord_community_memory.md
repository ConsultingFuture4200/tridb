# Case study: a Discord community memory on TriDB (gBrain v2)

**Status:** draft v0.1.0 — first real-workload deployment report
**Date:** 2026-07-20
**Workload owner:** gBrain v2 (ResonantOS / Augmentatism community), external repo
**Target:** Postgres 17 + `vector` 0.8.0 + `graph_store_am` 0.2.0 + `tjs_pg` 0.2.0
(release container `tridb/postgres-trimodal:pg17`), DGX Spark (ARM64/GB10) as the
production host, x86_64 workstation for development

---

**TL;DR** — We rebuilt a Discord community's memory service ("gBrain") as a
single-writer HTTP service in front of TriDB: every memory is a relational row
+ 768-d embedding + graph vertex written in **one transaction on one WAL**,
with dense ids (`ext_id == vid`) enforced at write time. On a 593-node /
2,086-edge community corpus with 20 planted questions, fused `tjs_open` recall
took multi-hop hit@10 from **2/6 (vector-only) to 6/6**, and anchored
filter-first recall answered **6/6** member-provenance questions that both
seedless modes missed (1/6). Vector-friendly questions tied (7/7 both) — the
fused operator is a multi-hop lever, not a universal ranking upgrade, and this
report keeps that framing. We also hit real limits worth upstreaming: no
delete surface, out-edge-only anchored traversal, and single-writer id
allocation by convention rather than by engine.

---

## 1. The workload

A community Discord server (text channels, voice calls, media) feeding a
persistent, queryable, graph-aware memory:

- **Node kinds:** `message`, `transcript_chunk`, `call_summary`,
  `speaker_turn`, `member`, `topic`, `decision`, `action_item`,
  `media_caption`, `doc`, plus anchor kinds `channel`, `call`, `media`.
- **Edge types (12, auto-registered via `graph_store.register_edge_type`):**
  `authored`, `replies_to`, `in_thread`, `in_channel`, `mentions`, `spoke_in`,
  `part_of_call`, `about`, `decided_in`, `assigned_to`, `supersedes`,
  `attached_to`.
- **Modalities:** everything is normalized to text before embedding
  (faster-whisper transcripts, VLM keyframe captions, OCR) — one 768-d
  nomic-embed-text space, brought to TriDB as ready-made vectors.
- **Query classes:** ambient similarity ("what's the kombucha temperature"),
  multi-hop provenance ("why was this venue chosen" when the reasons live at a
  reply-chain root), and member-anchored tri-modal recall ("what has @member
  said about X across text and calls").

The service doubles as a dogfooding exercise: every documented TriDB limit we
hit is supposed to become an issue or PR here.

## 2. Architecture: making the single-writer contract structural

TriDB's graph AM v1 and the MCP reference's dense-id allocation
(`max(id)+1`) assume **one writer**. Instead of hoping every ingest path
cooperates, gBrain makes the constraint structural:

```
discordbot (capture) ──> disk-backed JSON queue ──> ONE writer loop ──> TriDB
Claude sessions ──> MCP shim ──> gBrain HTTP (writes proxied, reads direct)
```

- **One process, one mutating connection.** Every write (HTTP `/memory`,
  `/edge`, `/ingest/event`) is a task in a disk-backed queue drained by a
  single serial loop. A crash mid-write leaves the task file on disk; the next
  start re-drains it.
- **One transaction per memory:** relational INSERT + vector column +
  `gph_upsert_vertex()` in the same transaction. The writer asserts
  `ext_id == vid` inside the transaction and **aborts on drift** — in normal
  operation the assert never fires, and having it turned the invariant from
  documentation into a mechanical guarantee.
- **Append-only edits:** Discord edits become a new memory + `supersedes`
  edge + a relational `superseded_by` shadow column that every recall filter
  leg excludes. Verified in the comparison run: the superseded version never
  surfaced in any mode.

### Experience report: single-writer + dense ids

The contract was *easy to live with* precisely because it was absorbed into
one small component. Things that worked well:

- The queue-in-front-of-one-writer topology cost ~200 lines and removed the
  entire class of "who allocates the next id" races. Dense ids fall out for
  free when one loop does `max(id)+1`.
- One-WAL atomicity is a genuine operational simplification: the crash-test
  suite (`make stock-crash-test PG_MAJOR=17`, run on the Spark before
  go-live) passed all 5 WAL recovery scenarios, and we have no torn-write
  reconciliation code anywhere in the service.
- Readers scale independently: recall, stats, and graph reads go straight to
  Postgres over a small pool (MVCC handles them), so the writer is never a
  read bottleneck.

Friction (each maps to an upstream item, §5):

- Any *second* tool that wants to write (an MCP server in a Claude session, a
  backfill script) must be taught to proxy through the service. A read-only
  MCP mode and/or server-side id allocation would shrink that blast radius.
- Deletions cannot be honored yet. A community DB **will** get removal
  requests; we currently append them to a pending-tombstones ledger and wait
  for an engine delete/tombstone surface.

## 3. Deployment on the target (ARM64 DGX Spark)

Phase-0 exit criteria, all green on the Spark:

- `make stock-release-smoke PG_MAJOR=17` — ARM64 release image built clean;
  direct `tjs_open` + canonical `graph_query` both passed.
- `make mcp-demo` — 5 memories, 2 edges, fused recall returned the planted
  memory in top-3, `graph_censored=False`.
- `make stock-crash-test PG_MAJOR=17` — 5/5 WAL crash-recovery scenarios
  (committed, uncommitted, tombstones) passed on target hardware.

No ARM64-specific build friction in the stock-PG release path — the container
built and the suites ran unmodified. (The forked-MSVBASE engine path was not
part of this deployment; gBrain runs on the stock-PG release container.)

## 4. Honest comparison: vector vs fused vs anchored on one store

Full method and per-question table live with the workload repo
(`docs/comparison-vector-vs-fused.v0.1.0.md`); summary here. Corpus: 593
memories / 2,086 typed edges seeded through the production write path; real
nomic-embed-text 768-d embeddings; k=10; gBrain defaults (`term_cond=64`,
`m_seeds=4`, `hops=2`, PPR fused scoring). 20 questions in four classes with
planted golds and deliberate near-topic noise (~370 ambient messages).

| Class | metric | vector | fused | anchored |
|---|---|---|---|---|
| vector-friendly (7) | hit@10 | 7/7 | 7/7 | — |
| multi-hop (6) | hit@5 | 2/6 | **5/6** | — |
| multi-hop (6) | hit@10 | 2/6 | **6/6** | — |
| member-anchored (6) | hit@10 | 1/6 | 1/6 | **6/6** |
| supersedes control (1) | old version leaked | no | no | — |

Honesty probes surfaced verbatim in every response: all fused runs terminated
`term_cond`, all anchored runs `filter_first`, `graph_censored=false`
throughout (corpus far below traversal budgets).

Reading it the way this repo's own benchmarks are read:

- **Ties are ties.** On lexically-matched questions fused ≈ vector (one
  rank-1→2 slip, one shared miss-at-5). Nobody should adopt the fused
  operator for that class.
- **The multi-hop lift is the same mechanism as the public HotpotQA result**
  (bridge injection admitting the out-edge reach of vector seeds; +15.6 pt
  joint recall@5 there): the questions vector loses are exactly those where
  the answer text shares no vocabulary with the question but is pointed at by
  the chatter that *does* match (`replies_to` roots). On this corpus that was
  a 2/6 → 6/6 hit@10 swing.
- **Anchored filter-first is the only mode that answers member-provenance
  questions** on this corpus (6/6 vs 1/6 for both seedless modes), including
  a cross-modal case answered from a voice-call speaker-turn while text
  channels held only bait. This validates the source-anchored query class as
  the decisive product surface, matching the repo's positioning.
- **Gold lands in top-5, not at #1.** Graph-recovered golds ranked 2–6; the
  fused score still leans on vector distance. For k≥4 agent consumption this
  is the answerable/unanswerable line, not a "gold always first" story.

## 5. Limits hit → upstream ledger

| Limit (observed in this deployment) | Proposed upstream item |
|---|---|
| **Out-edge-only reach in anchored/filter-first and seed expansion.** With natural ingest directions (content → anchor), topic/channel/call anchors are out-edge *sinks*: filter-first from them reaches nothing, and decision nodes are unreachable from the discussions that produced them (M3 in the comparison was only answerable via a member's authored speaker-turn). | Reverse/undirected traversal option for `gph_traverse_bounded` + the seed-reach leg — or documented guidance that applications must write reciprocal edges for anchor kinds they want to filter-first from. |
| **No delete/update surface** (engine tombstones exist; nothing exposed at the memory/SQL convenience layer). Community removal requests currently accumulate in an application-side pending ledger. | Design + PR exposing delete/tombstone through the memory surface (driven by real Discord deletion semantics). |
| **Dense-id allocation is by convention** (`max(id)+1` in the single writer). Safe here because the topology enforces one writer, but every second tool is a footgun. | Server-side allocation: sequence + trigger enforcing `ext_id == vid`, making multi-writer id-safe. |
| **MCP server has no read-only mode**, so interactive agent sessions must proxy writes through the service to preserve the single-writer contract. | `--read-only` flag (recall/neighbors/stats only) for safe second-process access. |
| **Logical backup needs the plan-099 procedure** (pg_dump alone silently loses graph topology). Not a defect — the procedure worked — but it is an extra operational step every deployment must know about. | Already documented in `INSTALL_stock_pg.md`; this deployment adds a real-workload restore-drill data point. |
| **No real-workload evidence in the benchmark suite.** | This document; and the corpus is the standing candidate for the GX10-gated 128 GB headline run once it grows to scale. |

## 6. What we did not measure

- **No latency claims.** The comparison ran on an x86 dev workstation
  container at toy scale; per the repo's own rules, raw-microsecond numbers
  from a standin at standin scale are not evidence.
- **The 128 GB headline benchmark remains not-run.** This corpus (hundreds of
  nodes today, growing through live Discord ingest) is the intended organic
  payload, gated on reaching meaningful scale on the GX10.
- **Synthetic corpus.** The fixture is deterministic and planted; it is
  designed to *represent* the community's query classes, not to be them. The
  live-corpus rerun of the same harness is the follow-up once opt-in backfill
  lands.
