# Plan 010: Make tjs_open's two ranking heaps share one distance metric, decide the bridge/vector blend policy, and wire its orphaned smoke test

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `advisor-plans/README.md` — unless a reviewer dispatched you and told you
> they maintain the index.
>
> **Drift check (run first)**: `git diff --stat 408e852..HEAD -- scripts/patches/tridb_tjs_open_operator.patch test/tjs_open_smoke.sql Makefile bench/tjs_open_ref.py`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: none (but land BEFORE advisor-plans/017 — same patch file)
- **Category**: bug
- **Planned at**: commit `408e852`, 2026-07-01

## Why this matters

`tjs_open` (the flagship v2 open-retrieval operator, ADR-0012 realization B, shipped as a fork
patch) ranks its final output by merging two heaps whose keys are in **different units**: the
vector-stream heap is keyed on hnswlib's **squared** L2 distance, while the bridge heap is keyed
on the **sqrt'd** (true Euclidean) distance returned by the patched scalar `l2_distance`. Set
membership of the top-k is unaffected (the heaps never compete on distance directly), but the
final nearest-first **emission order is wrong** whenever bridges and vector winners interleave —
any consumer that takes fewer than k rows, or that trusts the ordering, gets wrong results.
Separately, the finalization fills the answer **bridges-first up to k**, so on a well-connected
graph the emitted set can be 100% bridges and 0% pure vector winners — a divergence from the
operator's own documented contract ("emit top-k vector-ranked WITH the bridges injected") that
needs an explicit decision. Finally, this operator's only engine test (`test/tjs_open_smoke.sql`)
is wired into **nothing** — no Makefile target or script runs it — so none of this has regression
coverage.

## Current state

- `scripts/patches/tridb_tjs_open_operator.patch` — the entire operator ships as this 974-line
  fork patch (adds `src/tjs_open_operator.cpp` to the vendored MSVBASE tree at build time). The
  vendored tree (`vendor/MSVBASE/`) is gitignored and re-cloned by build scripts; **the patch file
  is the source of truth you edit**.
- Vector-stream key (squared L2). In `pullOneCandidateO` (patch, inside the new
  `src/tjs_open_operator.cpp`):

  ```c
  *rank_score = DatumGetFloat4(child->iss_ScanDesc->xs_orderbyvals[0]);
  ```

  The fork's HNSW scan populates that with hnswlib's raw metric — `vendor/MSVBASE/src/hnswindex.cpp:384`
  is `scan->xs_orderbyvals[0] = Float4GetDatum(result->GetDistance());`, and hnswlib's L2Space
  returns squared L2 (no sqrt).

- Bridge key (true Euclidean). In `fetchBridgeRowsO` (same patch), bridge rows are fetched via
  SPI with the distance computed by the scalar operator:

  ```c
  std::string sql = "SELECT " + *estate->attr_exp + ", (" + *estate->orderby_exp + ") AS __d FROM " + ...
  ...
  float dist = dnull ? std::numeric_limits<float>::infinity() : (float) DatumGetFloat8(dd);
  ```

  Outside an index scan, `<->` resolves to the patched scalar `l2_distance`
  (`scripts/patches/l2_distance_scalar.patch`), which ends `PG_RETURN_FLOAT8(std::sqrt(distance));`
  — i.e. **sqrt'd**.

- The mixed-unit sort. `finalizeTopKO` (same patch) drains both heaps and sorts the merged
  `chosen` vector on `.first` — comparing squared keys against sqrt'd keys:

  ```c
  // 1) bridges first (guaranteed), nearest bridges win when > k bridges exist.
  for (const auto& it : bridges_v) {
      if (chosen.size() >= k) break;
      if (chosen_ids.insert(id_of(it)).second)
          chosen.push_back(it);
  }
  // 2) fill remaining with vector winners not already chosen.
  ...
  std::sort(chosen.begin(), chosen.end(),
            [](const pq_item_o_t& a, const pq_item_o_t& b){ return a.first < b.first; });
  ```

  The bridges-first loop is also the blend-policy issue: `fetchBridgeRowsO` fetches the ENTIRE
  bridge set (bounded by `m_seeds * avg_deg^hops`, routinely ≥ k) into `bridge_pq` (capped at k),
  so step 1 alone can fill all k slots.

- `bench/tjs_open_ref.py` — the host-side Python reference implementation (frozen acceptance
  spec, tested by `tests/test_tjs_open_ref.py`). Its recall was 0.987 vs the C operator's 0.980 on
  HotpotQA.
- `test/tjs_open_smoke.sql` — the operator's only engine test. `grep -rn tjs_open_smoke Makefile
  scripts/ .github/` returns **nothing**: it is not in `ENGINE_TESTS` (Makefile line ~10) and no
  runner invokes it.
- Makefile engine-test surface (excerpt, `Makefile:9-12`):

  ```make
  ENGINE_TESTS := test/graph_store_test.sql test/trimodal_compose.sql \
                  test/trimodal_early_term.sql test/fork_distance_probe.sql \
                  test/vector_relaxed_mono_test.sql test/canonical_e2e_test.sql \
                  test/parse_canonical.sql test/hnsw_costestimate_unordered_test.sql
  ```

- Patch-chain verification: `scripts/ci_check_patches.sh` clones MSVBASE at the pinned commit and
  applies + sentinel-verifies the whole chain WITHOUT compiling — the fast gate for patch edits.
  Patch application lives in `scripts/lib/msvbase_patches.sh` (each `.patch` has a sentinel grep in
  `verify_patches()`).
- Repo conventions: commits are `type(scope): summary`; engine C targets PG 13.4 APIs; TR-1 (no
  blocking operators, Open/Next/Close + early termination) is a locked invariant — your fix must
  not add any new materialization.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Python tests | `make test` | exit 0, all pass |
| Lint | `make lint` | exit 0 |
| Patch chain applies | `bash scripts/ci_check_patches.sh` | exit 0, "all patches verified" style output |
| Engine image build (SLOW, ~9.5 GB; only if instructed) | `scripts/x86build.sh --docker` | image `tridb/msvbase:dev` |
| Engine suites (needs image) | `make graph-test` | all suites PASS |
| Run one engine SQL by hand (needs image) | `bash scripts/graph_test.sh tridb/msvbase:dev test/tjs_open_smoke.sql` | exit 0 |

## Scope

**In scope** (the only files you should modify):
- `scripts/patches/tridb_tjs_open_operator.patch`
- `test/tjs_open_smoke.sql` (extend)
- `Makefile` (one line: add the smoke test to `ENGINE_TESTS`)
- `advisor-plans/README.md` (your status row)

**Out of scope** (do NOT touch, even though they look related):
- `scripts/patches/tridb_tjs_operator.patch` — the single-source `tjs()` never mixes metrics
  (it ranks only on `xs_orderbyvals`); leave it alone.
- `bench/tjs_open_ref.py` / `tests/test_tjs_open_ref.py` — the frozen reference. Do not "align"
  it to the C code; it is the spec, not the implementation.
- `expandMultiSeedO`'s per-node SPI loop — that is plan 017's scope; do not restructure it here.
- Any other patch in `scripts/patches/`.

## Git workflow

- Branch: `advisor/010-tjs-open-metric-blend` from `origin/master`
- Commit style: `fix(tjs_open): unify heap distance metric to squared L2 (advisor plan 010)`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Unify the metric — square the bridge distance at fetch time

In `scripts/patches/tridb_tjs_open_operator.patch`, in `fetchBridgeRowsO`, change the key
assignment so the bridge heap uses the same squared-L2 unit as the vector stream:

```c
float dist = dnull ? std::numeric_limits<float>::infinity()
                   : (float) (DatumGetFloat8(dd) * DatumGetFloat8(dd));
```

(Read the datum once into a `double d8` local, then `dist = (float)(d8 * d8);` — don't call
`DatumGetFloat8` twice.) Add a one-line comment: `// __d is sqrt'd Euclidean (scalar l2_distance);
square it so bridge_pq shares vec_pq's squared-L2 unit (xs_orderbyvals[0]).`

You are editing a `.patch` file: keep the unified-diff line-count headers (`@@ -a,b +c,d @@`)
consistent. The edit replaces existing `+` lines with the same number of `+` lines if possible;
if you add lines, recompute the hunk's `+` count.

**Verify**: `bash scripts/ci_check_patches.sh` → exit 0 (chain still applies cleanly).

### Step 2: Decide and implement the blend policy (bounded bridge share)

The current `finalizeTopKO` gives bridges ALL k slots when ≥ k bridges exist. Change the fill to
reserve room for vector winners: bridges may take at most `k/2` slots (integer division, min 1
when any bridge exists), then vector winners fill the rest, then — only if vector winners run
out — remaining bridges backfill. Keep the id-based dedup exactly as it is. Update the comment
block above the function to state the policy: `bridges guaranteed up to k/2 slots; vector winners
keep the rest (ADR-0012 "injected", not "replaced")`.

Rationale to preserve in the comment: the ADR-0012 (B) contract is bridge *injection* into a
vector-ranked answer; bridges-take-all silently deletes the vector modality on dense graphs.

**Verify**: `bash scripts/ci_check_patches.sh` → exit 0.

### Step 3: Extend the smoke test to pin both fixes

Extend `test/tjs_open_smoke.sql` (match its existing style — it creates a small corpus, runs
`tjs_open(...)`, asserts output) with two assertions:

1. **Ordering assertion**: build a corpus where at least one bridge and one vector winner both
   land in the top-k with distances that would invert under the old mixed units (e.g. a bridge at
   true distance 5.0 and a vector winner at true distance 4.5 — old code sorts bridge key 5.0
   before squared key 20.25; fixed code sorts 25.0 after 20.25). Assert emission order via a
   `WITH ... SELECT array_agg(id)` against the expected id array.
2. **Blend assertion**: with a hub graph where the bridge set is ≥ k, assert the result contains
   at least one row that is NOT graph-reachable from the seeds (i.e. a pure vector winner
   survived).

Keep the corpus tiny (tens of rows) and deterministic (no random).

**Verify**: `make lint` → exit 0 (ruff also checks nothing here, but run it); if the engine image
exists on this machine (`docker image inspect tridb/msvbase:dev`), run
`bash scripts/graph_test.sh tridb/msvbase:dev test/tjs_open_smoke.sql` → exit 0. If the image does
not exist, mark the run "engine-gated: unbuilt here" in your report — do NOT claim it passed.

### Step 4: Wire the smoke test into ENGINE_TESTS

In `Makefile`, append `test/tjs_open_smoke.sql` to the `ENGINE_TESTS` list (line ~9-12, excerpt
in Current state).

**Verify**: `make -n graph-test | grep tjs_open_smoke` → prints a line containing
`test/tjs_open_smoke.sql`.

## Test plan

- The extended `test/tjs_open_smoke.sql` IS the regression test (ordering + blend assertions,
  Step 3). Model its structure on the existing content of the same file and on
  `test/canonical_e2e_test.sql` for the assert-via-exception pattern.
- Full gate where the image exists: `make graph-test` → all suites PASS including the new one.
- Python layer untouched: `make test` → same pass count as before your change.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `bash scripts/ci_check_patches.sh` exits 0
- [ ] `grep -n "d8 \* d8\|dist \* dist\|\* DatumGetFloat8" scripts/patches/tridb_tjs_open_operator.patch` shows the squaring at the bridge-fetch site
- [ ] `grep -n "k/2\|k / 2" scripts/patches/tridb_tjs_open_operator.patch` shows the bounded bridge share in `finalizeTopKO`
- [ ] `grep -n "tjs_open_smoke" Makefile` returns the ENGINE_TESTS entry
- [ ] `make test` and `make lint` exit 0
- [ ] Engine run: `make graph-test` PASS **or** an explicit "engine-gated: unbuilt here" note in your report
- [ ] No files outside the in-scope list modified (`git status`)
- [ ] `advisor-plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- The patch no longer contains `fetchBridgeRowsO` / `finalizeTopKO` as excerpted (drift).
- `ci_check_patches.sh` fails after your edit twice in a row (your hunk headers are likely wrong
  — report the apply error verbatim rather than force-fixing offsets).
- You find evidence that `xs_orderbyvals[0]` is NOT squared in this fork (e.g. a sqrt applied in
  `vendor/MSVBASE/src/hnswindex_scan.cpp`) — the whole premise changes; report.
- The blend policy change makes the live HotpotQA recall (if you are asked to run
  `make tjs-open-live`) drop below 0.95 — the k/2 reservation may need tuning; report the number
  instead of picking a new fraction yourself.

## Maintenance notes

- Plan 017 (batched BFS) edits the same patch file — land this first; 017 must rebase over it.
- The PPR+FR+RRF refinement (ADR-0012 addendum, host-validated 0.987) will REPLACE the blend
  policy chosen in Step 2 when it lands — the k/2 rule is an interim honesty fix, not the end
  state. Reviewer should scrutinize: hunk-header arithmetic in the patch, and that the ordering
  assertion in the smoke test actually discriminates (would fail on the pre-fix code).
- Deferred out of this plan: a C-vs-`tjs_open_ref.py` parity harness on a pinned corpus (a good
  follow-on once the blend policy is stable; see the ranked findings in advisor-plans/README.md).
