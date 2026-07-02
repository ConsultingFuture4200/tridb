# Plan 014: Harden the benchmark grading layer — completeness gates in every grader, one baseline-merge semantics, reader-failure accounting, one recall_at_k

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `advisor-plans/README.md` — unless a reviewer dispatched you and told you
> they maintain the index.
>
> **Drift check (run first)**: `git diff --stat 408e852..HEAD -- bench/ tools/real_corpus.py tools/sweep_corpus.py baseline/sm2.py tests/`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED (touches published-number code paths — golden-value tests gate every change)
- **Depends on**: none
- **Category**: bug / tech-debt
- **Planned at**: commit `408e852`, 2026-07-01

## Why this matters

The bench layer's outputs are TriDB's published claims, and it has already produced one corrupted
headline (the predicate-blind SM-2 number). Four hardening gaps remain: (1) three graders compute
headline metrics from engine transcripts **without checking the run completed** — a mid-run
segfault silently becomes a plausible smaller-N number; (2) two modules both documented as "the
realized canonical baseline" disagree on whether a qualifying answer must also appear in the ANN
over-fetch set, so SM-4 parity from the two pipelines isn't comparable; (3) a failed LLM reader
call is indistinguishable from a wrong answer (scores 0), silently deflating EM/F1 headlines; and
(4) `recall_at_k` — the defining metric — is re-implemented in ~10 places with subtly different
empty-oracle semantics, so a semantics fix doesn't propagate.

## Current state

1. **DONE-gates**. The transcripts emit sentinel markers, and two graders enforce them:
   - `bench/sm2_compare.py:320-321`:
     ```python
     if "#SM2 DONE" not in text:
         raise SystemExit("TriDB transcript did not reach '#SM2 DONE' — incomplete")
     ```
   - `bench/live_report.py:213-214`: same pattern for `#BENCH DONE`.
   Three graders emit a marker but never check it:
   - `bench/v2a_open.py` — script writes `w("\\echo #V2A DONE")` (line ~79); `grade()` (line ~102)
     computes `recall_at_k` from whatever parsed.
   - `bench/tjs_open_live.py` — writes `#TJSOPEN DONE` (line ~75); grader never checks.
   - `bench/h2h_report.py` — writes `#H2H DONE` (line ~92); `parse_tridb`/grading (line ~145)
     never checks.
   Also check `bench/filtered_report.py` and `tools/sweep_corpus.py` `report()` — at plan time
   neither validated a marker; `filtered_report`'s main parses `args.raw.read_text()` directly.
2. **Two baseline-merge semantics**:
   - `baseline/sm2.py:328-334` (`merge_canonical`) — ANN-pruned:
     ```python
     for dst in kept_dst:
         if dst in reached and dst in vector_dist:
             survivors.append((vector_dist[dst], dst))
     ```
     (a qualifying dst outside the `k*32` ANN over-fetch is dropped).
   - `bench/live_report.py:185-196` (`baseline_query_canonical`) — exact:
     ```python
     ranked_dst = sorted(kept.keys(), key=lambda d: _l2_sq(corpus.entities[d]["embedding"], q_emb))
     ```
     (never intersects with `vector_hits`; the over-fetch set feeds only `peak_intermediate_rows`).
   Both docstrings claim to model the realized canonical query. The live SM-2/SM-4 head-to-head
   (`sm2_compare`) grades against the pruned model; the in-process report against the exact one.
3. **Reader failure = 0**. `bench/graphrag_report.py` `CodexReader.answer` (~line 300):
   ```python
   except Exception:  # noqa: BLE001 — a failed/timed-out call scores 0, run continues
       return ""
   ```
   The empty string then scores EM=0/F1=0 downstream; no failure tally exists.
4. **recall_at_k copies**. Canonical (docstring'd, tests pin it): `tools/real_corpus.py:361`:
   ```python
   def recall_at_k(returned, oracle, k=None) -> float:
       ...
       if not truth:
           return 1.0 if not got else 0.0  # empty truth: perfect only if nothing returned
       return len(got & set(truth)) / len(truth)
   ```
   Independent copies (verified sample): `bench/recall_decay.py:52 _recall`,
   `tools/sweep_corpus.py:192 _recall`, `bench/h2h_report.py:130 _recall`,
   `bench/ablation_report.py:~204`, `bench/rabitq_sim.py:~235`, plus inline slicing in
   `bench/v2a_open.py`, `bench/tjs_open_live.py`, `bench/filtered_report.py`,
   `bench/tjs_open_ref.py`. `bench/metrics.py` (the module housing the SM-1..SM-5 metric family
   and `_jaccard`/`_safe_ratio`) has NO recall function.
5. Conventions: pytest in `tests/` (`make test`, 143 tests at plan time), ruff (`make lint`),
   pure-Python 3.12. Tests pin semantics with golden values — see `tests/test_sweep_corpus.py:143`
   ("semantics tools/real_corpus.recall_at_k must match") as the pattern.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `make test` | exit 0 (count grows by your new tests) |
| Lint | `make lint` | exit 0 |
| Single test file | `.venv/bin/python -m pytest tests/test_bench_metrics.py -q` | pass |
| Golden re-run of a grader (no engine) | `.venv/bin/python -m pytest tests/ -q -k "sm2 or h2h or v2a"` | pass |

## Scope

**In scope**:
- `bench/v2a_open.py`, `bench/tjs_open_live.py`, `bench/h2h_report.py`, `bench/filtered_report.py`,
  `tools/sweep_corpus.py` (DONE-gates only)
- `bench/metrics.py` (add `recall_at_k` delegating home)
- `bench/recall_decay.py`, `bench/ablation_report.py`, `bench/rabitq_sim.py` (repoint to the
  shared function)
- `baseline/sm2.py` + `bench/live_report.py` (docstrings + the unification decision, Step 3)
- `bench/graphrag_report.py` (failure tally)
- `tests/` (new/extended tests)
- `advisor-plans/README.md` (status row)

**Out of scope**:
- `bench/tjs_open_ref.py` — frozen acceptance spec for the C operator; do not repoint its
  internal recall (it is itself pinned by tests).
- `tools/real_corpus.py:recall_at_k` — stays the semantic source; you may re-export it, not edit it.
- Any SQL-generation change that would alter what the engine runs.
- `bench/results/*` — generated artifacts; never hand-edit.

## Git workflow

- Branch: `advisor/014-bench-grading-hardening` from `origin/master`
- Commits per step: `fix(bench): DONE-completeness gates in all graders (advisor plan 014)` etc.
- Do NOT push or open a PR unless instructed.

## Steps

### Step 1: DONE-gates everywhere

In each of the five ungated modules, add the exact `sm2_compare.py:320` pattern at the top of the
function that first receives the raw transcript text (grade/parse/report/main — pick the earliest
single choke point per module), with the module's own marker (`#V2A DONE`, `#TJSOPEN DONE`,
`#H2H DONE`, `#FILT DONE`, `#SWEEP DONE` — confirm each marker string by grepping the module's
script-generation code; use exactly what it emits).

**Verify**: `grep -n "DONE' not in\|DONE\" not in" bench/v2a_open.py bench/tjs_open_live.py bench/h2h_report.py bench/filtered_report.py tools/sweep_corpus.py` → one hit per file; `make test` → pass.

### Step 2: Tests for the gates

For each module add/extend a test in `tests/` (model on how `tests/test_sm2_compare.py` feeds
synthetic transcript text): a truncated transcript (marker absent) must raise `SystemExit` with
"incomplete" in the message; a complete one must grade normally.

**Verify**: `make test` → pass, N≥5 new tests.

### Step 3: Unify the baseline-merge semantics

Decision (made by the advisor, encode it): the baseline models **real Milvus ANN behavior**, so
the ANN-pruned rule (`baseline/sm2.py`) is the canonical semantics — a real multi-system baseline
cannot return an answer its vector store never surfaced. Change
`bench/live_report.py:baseline_query_canonical` to intersect with `vector_hits` the same way
(`if d in vector_hits` when ranking `kept`), and update BOTH docstrings to say: "ANN-pruned
merge: models the real Milvus over-fetch (k*32); the exact-oracle variant lives only in
`bench/harness.py:baseline_query_inprocess`'s spec-model." Then check
`bench/harness.py:baseline_query_inprocess` — it already prunes via `s in src_dist`; leave it,
but fix its docstring if it claims otherwise.

**IMPORTANT — number impact**: this can change `report_live`'s SM-4/parity outputs. Before the
change, run whatever grading tests exist (`pytest -k live_report`) and capture current golden
values; update goldens in the same commit with a message noting the semantic unification.

**Verify**: `make test` → pass; `grep -n "vector_hits" bench/live_report.py` shows the
intersection in the ranking path.

### Step 4: Reader-failure accounting

In `bench/graphrag_report.py`, make reader failures countable: the `except Exception` path
returns a sentinel (e.g. `None`) instead of `""`; the scoring loop counts `None` as
`reader_failures += 1` and EXCLUDES those questions from the EM/F1 denominator, emitting
`reader_failures` in the JSON summary and a stderr warning when > 0. An empty-but-successful
answer still scores 0 (unchanged).

**Verify**: `make test` → pass (extend `tests/test_graphrag.py` with a fake reader raising once:
summary shows `reader_failures == 1` and the denominator shrinks by 1).

### Step 5: One recall_at_k

Add to `bench/metrics.py`:
```python
from tools.real_corpus import recall_at_k  # single-source semantics (empty truth: 1.0 iff nothing returned)
```
(or a thin wrapper if the import direction violates layering — `bench` already imports from
`tools` elsewhere; verify with `grep -rn "from tools" bench/ | head`). Repoint the three
straightforward copies (`bench/recall_decay.py:_recall`'s final scoring line,
`tools/sweep_corpus.py:_recall`, `bench/h2h_report.py:_recall`) to call it, DELETING the local
definitions. Leave `ablation_report`/`rabitq_sim`/inline sites for a follow-up ONLY if their
semantics differ (check each: if a copy's empty-oracle or dedup behavior differs from canonical,
do NOT silently change the number — record the divergence in your report and leave that copy).

**Verify**: `make test` → pass with identical golden values for untouched-semantics modules;
`grep -rn "def _recall" bench/ tools/` → only the copies you deliberately left, each noted in
your report.

## Test plan

- Step 2: 5 truncated-transcript tests (one per gated module).
- Step 3: golden-value update for live_report parity, committed with rationale.
- Step 4: reader-failure tally test in `tests/test_graphrag.py`.
- Step 5: existing pinning tests (`tests/test_sweep_corpus.py` "must match" test) keep passing —
  they are the migration guard.
- Full: `make test && make lint` → exit 0.

## Done criteria

- [ ] All five graders raise on a missing DONE marker (greps + tests)
- [ ] `live_report` and `sm2` share the ANN-pruned merge; docstrings updated; goldens updated in
      the same commit with rationale
- [ ] `graphrag_report` emits `reader_failures` and excludes failures from the EM/F1 denominator
- [ ] `bench/metrics.py` exposes the single `recall_at_k`; ≥3 duplicate definitions deleted
- [ ] `make test && make lint` exit 0; `git status` clean outside scope
- [ ] `advisor-plans/README.md` status row updated

## STOP conditions

- A DONE-marker string in the module's emitter differs from this plan's list (use the emitted
  string; if a module emits NO marker at all, add the emitter line too — but if the transcript
  format has changed materially, report).
- Step 3's golden-value change moves SM-4 parity by more than 1 query on the pinned fixtures —
  that magnitude suggests the fixtures encode the exact-oracle assumption deeply; report the
  before/after instead of updating goldens.
- Any recall copy's semantics differ from canonical in a way that changes a stored
  `bench/results/*.json` number — report; do not regenerate results files.

## Maintenance notes

- New bench modules MUST use `bench.metrics.recall_at_k` and the DONE-gate pattern — reviewer
  should reject future modules that hand-roll either.
- The exact-oracle baseline variant now lives only in the stub path (`bench/harness.py`); if
  anyone reports "SM-4 differs between make bench and make sm2", the pruning rule is the first
  place to look (documented in both docstrings after Step 3).
- Deferred: consolidating `vec_literal` copies and the docker-exec shell boilerplate (recorded in
  advisor-plans/README.md ranked findings), and the ablation single-token seed question (own row).
