# Plan 017: Batch tjs_open's graph expansion — one SPI round trip per hop instead of one per frontier node, and fail loud on SQL-buffer truncation

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `advisor-plans/README.md` — unless a reviewer dispatched you and told you
> they maintain the index.
>
> **Drift check (run first)**: `git diff --stat 408e852..HEAD -- scripts/patches/tridb_tjs_open_operator.patch test/tjs_open_smoke.sql`
> Plan 010 edits the same patch — expect drift there; re-locate the excerpts in the CURRENT
> patch before editing. On a material shape change (functions renamed/removed), STOP.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED (hot-path operator change; behavior must be bit-identical on results)
- **Depends on**: advisor-plans/010 (same patch file — land 010 first, rebase this over it)
- **Category**: perf
- **Planned at**: commit `408e852`, 2026-07-01

## Why this matters

`tjs_open`'s multi-source BFS issues **one `SPI_execute` per frontier node per hop**, each with a
freshly `snprintf`'d SQL string — a full parse+plan+execute cycle per node. With defaults
(`m_seeds=5, hops=2`) and any realistic hub degree, that is hundreds-to-thousands of SPI round
trips before the operator emits its first row; the cost grows multiplicatively with `hops`. The
expansion is the operator's dominant graph-leg latency and it is pure overhead — the same
frontier can be expanded in **one** SPI call per hop. Separately, the operator builds its
vector-leg SQL into a fixed 100 KB stack buffer with `snprintf`, which **silently truncates** on
overflow (very large `filter_exp`, e.g. a long `timestamp IN (...)` list, or a high-dim vector
literal in `orderby_exp`) — producing a confusing downstream error instead of a clean rejection.

## Current state

- `scripts/patches/tridb_tjs_open_operator.patch` — the operator ships as this fork patch (adds
  `src/tjs_open_operator.cpp`; the vendored tree is gitignored — **edit the patch file**).
- The per-node loop (`expandMultiSeedO`, verified):

  ```c
  for (int h = 0; h < hops && !frontier.empty(); h++) {
      std::vector<int64> next;
      for (int64 u : frontier) {
          char cmd[256];
          snprintf(cmd, sizeof(cmd),
                   "SELECT dst FROM graph_store.neighbors(%lld) AS dst", (long long) u);
          int ret = SPI_execute(cmd, true /* read_only */, 0);
          ...
          for (uint64 i = 0; i < proc; i++) { ... if (bridges.insert(v).second) next.push_back(v); }
      }
      frontier.swap(next);
  }
  ```

  Constraints that MUST be preserved (documented in the patch's own comments): the probe runs
  inside the SRF's single already-open SPI connection; `graph_store.neighbors` is itself an SRF
  doing nested SPI — **never open a sibling `SPI_connect`** (the fork's signature crash,
  DEV-1236); the bridge set stays bounded by `m_seeds * avg_deg^hops` (TR-1 — no corpus-sized
  materialization).
- The fixed buffer (verified, in the `tjs_open(PG_FUNCTION_ARGS)` first-call block):

  ```c
  char sourceText[102400];
  if ( strlen(text_to_cstring(filter_exp_text)) == 0 ){
      snprintf(sourceText, sizeof(sourceText), "select %s from %s order by %s", ...);
  } else {
      snprintf(sourceText, sizeof(sourceText), "select %s from %s where %s order by %s", ...);
  }
  ```

  (`tjs_operator.patch` has the same shape; out of scope here — note it in your report if asked.)
- `fetchBridgeRowsO` in the same file already demonstrates the batch idiom this plan needs: it
  builds `WHERE <id_col> = ANY (ARRAY[id1,id2,...])` as a `std::string` and issues ONE
  `SPI_execute`.
- Fast verification without compiling: `bash scripts/ci_check_patches.sh` (applies + sentinel-
  verifies the chain against the pinned MSVBASE clone).
- Engine test: `test/tjs_open_smoke.sql` (wired into `ENGINE_TESTS` by plan 010).

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Patch chain applies | `bash scripts/ci_check_patches.sh` | exit 0 |
| Python layer | `make test && make lint` | exit 0 |
| Engine suite (needs image) | `make graph-test` | all PASS incl. tjs_open smoke |
| Live recall check (needs image + data; optional) | `make tjs-open-live` | recall unchanged vs pre-change run |

## Scope

**In scope**:
- `scripts/patches/tridb_tjs_open_operator.patch`
- `test/tjs_open_smoke.sql` (extend with a multi-hop equivalence assert)
- `advisor-plans/README.md` (status row)

**Out of scope**:
- `scripts/patches/tridb_tjs_operator.patch` — single-source, single-probe; nothing to batch.
- `src/graph_store_ext/graph_store.c` (`neighbors` itself) — you batch the CALLER; do not add a
  new set-returning `neighbors(bigint[])` to the extension in this plan (that's a v0 surface
  change with its own consumers; the ANY-array form below needs no new SQL function).
- The finalize/heap logic (plan 010's territory), the seed window, term_cond semantics.

## Git workflow

- Branch: `advisor/017-tjs-open-batched-bfs` from `origin/master` (AFTER 010 merged; else from
  010's branch and say so)
- Commit: `perf(tjs_open): one SPI round trip per BFS hop via LATERAL neighbors (advisor plan 017)`
- Do NOT push or open a PR unless instructed.

## Steps

### Step 1: Batch the frontier expansion

Rewrite the inner loop of `expandMultiSeedO` so each hop issues ONE SPI call over the whole
frontier, using a LATERAL join over an unnest'd id array (no new SQL functions needed):

```c
// Build: SELECT n.dst FROM unnest(ARRAY[u1,u2,...]::bigint[]) AS f(src),
//        LATERAL graph_store.neighbors(f.src) AS n(dst)
std::string ids;
for (size_t i = 0; i < frontier.size(); i++) {
    if (i) ids += ",";
    ids += std::to_string((long long) frontier[i]);
}
std::string cmd = "SELECT n.dst FROM unnest(ARRAY[" + ids +
                  "]::bigint[]) AS f(src), LATERAL graph_store.neighbors(f.src) AS n(dst)";
int ret = SPI_execute(cmd.c_str(), true /* read_only */, 0);
```

Keep everything else identical: same `SPI_OK_SELECT` check (error message should include the
hop number), same `bridges.insert(v).second → next.push_back(v)` dedup/discovery logic (BFS
semantics are order-insensitive here because `bridges` dedups — result set is provably the same).
Use `std::string` (heap) for `cmd`, not a stack buffer — frontiers can be large. Update the
function's comment block to describe the batched form and why LATERAL (one parse/plan per hop;
neighbors' own nested SPI still nests under the single open connection).

Patch-file mechanics: keep hunk `+`-line counts consistent; run the verify below after every
hunk edit.

**Verify**: `bash scripts/ci_check_patches.sh` → exit 0.

### Step 2: Fail loud on sourceText truncation

Immediately after each `snprintf(sourceText, ...)` in the first-call block, check the return
value and error cleanly:

```c
int __n = snprintf(sourceText, sizeof(sourceText), ...);
if (__n < 0 || (size_t) __n >= sizeof(sourceText))
    ereport(ERROR, (errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
            errmsg("tjs_open: generated vector-leg SQL exceeds %zu bytes "
                   "(filter/orderby expression too large)", sizeof(sourceText))));
```

**Verify**: `bash scripts/ci_check_patches.sh` → exit 0;
`grep -n "PROGRAM_LIMIT_EXCEEDED" scripts/patches/tridb_tjs_open_operator.patch` → both snprintf
sites guarded.

### Step 3: Equivalence assert in the smoke test

Extend `test/tjs_open_smoke.sql` with a multi-hop case on a small deterministic graph (3+ hops of
structure, ≥2 seeds with overlapping neighborhoods so the dedup path is exercised): assert the
returned id set for `hops=2` equals the hand-computed expected set (the batched BFS must be
result-identical to the per-node BFS). Follow the file's existing assert style.

**Verify**: engine-gated — `bash scripts/graph_test.sh tridb/msvbase:dev test/tjs_open_smoke.sql`
if the image exists (else report "engine-gated: unbuilt here").

## Test plan

- Step 3's set-equivalence assert is the correctness regression.
- Where the image exists: `make graph-test` → PASS; optionally a before/after timing note from
  `make tjs-open-live` (recall must be IDENTICAL; latency should drop — report both numbers).
- `make test && make lint` unchanged.

## Done criteria

- [ ] `expandMultiSeedO` contains no per-node `SPI_execute` (grep the patch: `neighbors(%lld)` →
      no matches inside that function; the LATERAL form present)
- [ ] Both `sourceText` snprintf sites guarded with the truncation ereport
- [ ] Smoke test extended with the multi-hop set-equivalence assert
- [ ] `bash scripts/ci_check_patches.sh` exits 0; `make test && make lint` exit 0
- [ ] Engine run PASS or explicit "engine-gated: unbuilt here"
- [ ] `git status` clean outside scope; `advisor-plans/README.md` row updated

## STOP conditions

- Plan 010 has not landed and its branch conflicts materially with these hunks — coordinate
  ordering rather than resolving semantic conflicts yourself.
- The LATERAL form fails inside the fork's SPI (PG 13.4 supports LATERAL + unnest; if the fork's
  planner rejects it in this context, report the exact error — fall back is `SPI_prepare` once +
  `SPI_execute_plan` per node, but confirm with the maintainer before switching strategy).
- The smoke-test set-equivalence fails — the batched BFS changed reachability; report, do not
  adjust the expected set.
- Frontier id-list strings approaching tens of MB (pathological graphs) — if you find yourself
  chunking the array, cap the batch at e.g. 10k ids per call and note it; if that feels wrong,
  report.

## Maintenance notes

- When plan 016's rewire lands (operators → v1 `gph_neighbors`), this batched call site is the
  ONE place to update — keep the SQL string construction in a single helper for that reason.
- Reviewer: check hunk arithmetic, that `cmd` moved off the stack, and that the error message on
  SPI failure still identifies the hop.
- Deferred: the same 100KB-buffer guard for `tjs_operator.patch` (identical shape; fold into the
  next change that touches that file).
