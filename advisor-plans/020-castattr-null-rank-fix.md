# Plan 020: Fix the NULL-rank-scores-as-nearest-neighbor bug in TriDB's tjs/tjs_open operators (castAttrToFloatT)

> **Executor instructions**: Follow step by step; run every verification command. Stop and report
> on any "STOP condition". Update `advisor-plans/README.md` when done.
>
> **Drift check (run first)**: `git diff --stat 408e852..HEAD -- scripts/patches/tridb_tjs_operator.patch scripts/patches/tridb_tjs_open_operator.patch` — if either changed (plans 010/011/017 touch them), re-locate `castAttrToFloatT` in the current patch before editing.

## Status

- **Priority**: P2 (correctness on the canonical operator; MED confidence it fires in practice)
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none, but coordinate ordering with plans 010/011/017 (same patch files)
- **Category**: bug
- **Planned at**: commit `408e852`, 2026-07-01
- **Upstream origin**: `src/topk.cpp` / `src/multicol_topk.cpp` `castAttrToFloat` (inherited pattern)

## Why this matters

MSVBASE's `castAttrToFloat` maps a NULL rank score to `0.0`
(`src/topk.cpp:61-67`: `if (isNull) return 0;`). The value is used directly as the priority-queue
key, and the queue is min-by-distance — so `0.0` is the **best possible distance**. Any candidate
whose rank expression evaluates to SQL NULL is therefore ranked as a **nearest neighbor**,
silently displacing genuine matches, indistinguishable from a true zero-distance hit.

TriDB forked this helper verbatim into `castAttrToFloatT` (`src/tjs_operator.cpp:119`, with the
`if (isNull) return 0;` at ~124-125), and it feeds the **canonical composed operator's** ranking
path; `tjs_open` uses the same operator family. So a NULL sub-distance in a fused rank expression —
a NULL vector column, NULL arithmetic in fusion, a filtered-away leg — is scored as the closest
result. The single-column path is unaffected (it ranks on `xs_orderbyvals`, not this helper), which
is why it hasn't surfaced; the multi-term / rank-expression path is exposed.

## Current state

- The operators ship as fork patches (vendor tree gitignored — edit the `.patch` files):
  - `scripts/patches/tridb_tjs_operator.patch` — adds `src/tjs_operator.cpp`, containing
    `inline float castAttrToFloatT(TupleTableSlot *slot, int attno)` (~line 119) with the NULL→0
    return.
  - `scripts/patches/tridb_tjs_open_operator.patch` — the `tjs_open` operator; check whether it
    has its own `castAttrToFloatO`/uses `castAttrToFloatT` (grep the patch) and fix accordingly.
- The min-by-distance queue comparator: `pq_item_compare_lt` (keep-smallest), so a smaller key =
  ranked nearer. The helper's return is the rank key on the rank-expression path (the
  `children->size() > 1` branch), per `src/tjs_operator.cpp` (mirror of `topk.cpp:458`).
- Convention: fork patches wired via `scripts/lib/msvbase_patches.sh`; fast gate
  `bash scripts/ci_check_patches.sh`.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Patch chain applies | `bash scripts/ci_check_patches.sh` | exit 0 |
| Python layer | `make test && make lint` | exit 0 |
| Engine suite (gated) | `scripts/x86build.sh --docker && make graph-test` | PASS |

## Scope

**In scope**:
- `scripts/patches/tridb_tjs_operator.patch`
- `scripts/patches/tridb_tjs_open_operator.patch` (only if it carries its own NULL→0 helper)
- `test/canonical_e2e_test.sql` or `test/tjs_open_smoke.sql` (add a NULL-rank assertion)
- `advisor-plans/README.md` (status row)

**Out of scope**:
- Upstream `src/topk.cpp` / `src/multicol_topk.cpp` — TriDB doesn't route the PostgresMain multicol
  path (plan 018 removes the rewriter), and these are pristine-upstream; fixing the T-fork covers
  the canonical operator. Note the upstream copies in your report but don't patch them.
- The metric-unit / blend logic (plan 010).

## Git workflow

- Branch: `advisor/020-castattr-null-rank` from `origin/master`
- Commit: `fix(tjs): treat NULL rank score as +inf, not nearest neighbor (advisor plan 020)`
- Do NOT push or open a PR unless instructed.

## Steps

### Step 1: Fix the NULL return in castAttrToFloatT (and the tjs_open sibling if present)

Change `if (isNull) return 0;` to return `std::numeric_limits<float>::infinity()` (add
`#include <limits>` if not already present in the patched file), so a NULL-scoring candidate sorts
LAST (worst distance) and is evicted from the bounded top-k rather than promoted. Add a one-line
comment: `// NULL rank -> +inf: never rank a NULL-scoring row as a near neighbor (advisor 020)`.

Keep unified-diff hunk `+`-counts consistent.

**Verify**: `grep -n "infinity" scripts/patches/tridb_tjs_operator.patch` → present at the helper;
`bash scripts/ci_check_patches.sh` → exit 0.

### Step 2: Regression assertion

In `test/canonical_e2e_test.sql` (tjs) — add a small corpus row whose rank expression evaluates to
NULL (e.g. a NULL embedding component feeding the fused distance) and assert it does NOT appear in
the top-k, while genuine near rows do. Model on the file's existing assert style. If `tjs_open`
carried its own helper, add the analogous case in `test/tjs_open_smoke.sql`.

**Verify**: engine-gated — run under the image if present; else "engine-gated: unbuilt here".

## Test plan

- The NULL-rank exclusion assertion is the regression.
- `bash scripts/ci_check_patches.sh` + `make test && make lint` green.

## Done criteria

- [ ] `castAttrToFloatT` (and the tjs_open sibling if any) return +inf on NULL, not 0
- [ ] `bash scripts/ci_check_patches.sh` exits 0
- [ ] NULL-rank regression added; engine run PASS or "engine-gated: unbuilt here"
- [ ] `make test && make lint` exit 0; `git status` clean outside scope
- [ ] `advisor-plans/README.md` row updated

## STOP conditions

- The rank-expression path in the T-fork turns out NOT to use `castAttrToFloatT` for the queue key
  (i.e. it always ranks on `xs_orderbyvals`, like single-column topk) — then the bug can't fire via
  tjs; report that and downgrade to fixing the helper defensively without the test claim.
- `+inf` breaks an existing passing test (a test that depended on NULL→0) — report; that test
  encodes the bug.

## Maintenance notes

- Reviewer: confirm the comparator treats `+inf` as worst (it's min-by-distance, so yes) and that no
  code special-cases `0.0` as a sentinel elsewhere.
- If plan 018 (rewriter removal) lands, the upstream `castAttrToFloat` copies become fully dead on
  TriDB's path — no need to patch them.
