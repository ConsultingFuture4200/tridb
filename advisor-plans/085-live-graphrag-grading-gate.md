# Plan 085: Grade live GraphRAG output before reporting completion

> **Executor instructions**: Do not print a completion marker unless parsing and grading succeed.
> Engine/GX10 execution remains gated; host parser tests are required everywhere. Skip the advisor
> index update.
>
> **Drift check (run first)**:
> `git diff --stat a780b46..HEAD -- scripts/bench_graphrag.sh scripts/bench_graphrag_h2h.sh bench/live_report.py bench/graphrag_report.py bench/ Makefile tests/ docs/`

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: 072
- **Category**: bug / tests / docs
- **Planned at**: commit `a780b46`, 2026-07-15

## Why this matters

The live GraphRAG script captures engine output, prints a TODO for the grading step, then announces
`DONE`. It therefore proves only that SQL ran, not that all questions produced parseable IDs or that
retrieval/answers match the corpus. A green completion marker must be downstream of strict transcript
validation and measured grading.

## Current state

- `scripts/bench_graphrag.sh:82-85` says grading is TODO and immediately prints DONE.
- `bench/live_report.py:41-100` parses `#BENCH` IDs/examined/latency, and `build_report` requires
  `#BENCH DONE` plus complete per-qid observations.
- `bench/graphrag_report.py:86` loads the HotpotQA slice; line 182 exposes `evidence_scores`; the same
  module contains deterministic/LLM reader and answer EM/F1 helpers.
- `scripts/bench_graphrag_h2h.sh:52-58` already gates completion on its report/grader. It is a
  separate live multi-system comparison and must not be implied by the engine-only script.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused | `.venv/bin/pytest tests/test_graphrag_live_report.py -q` | all pass |
| Shell | `bash -n scripts/bench_graphrag.sh` | exit 0 |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope**:
- `scripts/bench_graphrag.sh`
- `bench/graphrag_live_report.py` (create)
- `tests/test_graphrag_live_report.py` (create)
- `Makefile` only if output arguments/targets need wiring
- Existing GraphRAG run docs only to distinguish engine-only from `graphrag-h2h`

**Out of scope**:
- Claiming the live multi-system baseline ran from `graphrag-live`.
- Changing retrieval SQL/operator semantics or benchmark corpus.
- Fabricating reader answers when no reader is configured.
- Claiming full-corpus/GX10 numbers from an x86 stand-in.

## Git workflow

Use assigned `dustin/dev-NNNN`; suggested commit:
`fix(bench): grade live graphrag output`.

## Steps

### Step 1: Specify and test strict transcript completeness

Create `bench/graphrag_live_report.py` around `live_report.parse_bench_output` and
`graphrag_report.load_slice`. Require `#BENCH DONE`, exactly every manifest qid, one result-ID record
and examined count per qid, integer IDs within corpus range, no duplicate conflicting records, and no
unexpected qids. Add tests for each rejection before wiring the shell.

**Verify**: complete synthetic transcript passes; missing DONE/qid, malformed IDs, duplicate conflict,
and out-of-range ID each exit nonzero with a specific message.

### Step 2: Compute evidence and answer metrics from live IDs

For each qid, grade retrieved IDs with existing `evidence_scores`; aggregate recall, joint recall,
and evidence F1 using the same reducers as `graphrag_report.py`. Build contexts from those exact live
IDs and run the configured existing reader; compute answer EM/F1 with existing normalization. If the
reader is unavailable, fail the answer-grade mode or emit an explicitly evidence-only report selected
by a flag; never label absent answer grading as EM/F1 success.

Emit stable JSON plus Markdown containing corpus identity, k/term_cond, qid count, evidence metrics,
answer reader name/metrics or explicit evidence-only status, examined statistics, and `engine_live`.

**Verify**: synthetic fixtures with known gold IDs/answers produce exact expected aggregate numbers;
reader failure behavior is tested and cannot reduce the denominator silently.

### Step 3: Put the grader in the shell's success path

After the Docker pipeline closes `RAW`, invoke the new module with manifest/raw/output paths. Keep
raw and reports in a configurable persistent results directory rather than deleting the only evidence
on failure; use a temp workdir only for generated SQL. Print `[graphrag-live] DONE` only after the
grader exits 0 and reports all expected questions. On failure, print artifact paths and exit nonzero.

**Verify**: use synthetic raw injection or a test seam to show incomplete output prevents DONE and
returns nonzero; complete output prints DONE once.

### Step 4: Correct run-scope documentation

State that `graphrag-live` grades the live TriDB engine only. Point measured multi-system latency
comparison to `make graphrag-h2h`; do not say the engine-only script runs that baseline.

**Verify**: `rg 'TODO\(GX10\).*wire|DONE \(engine-gated' scripts/bench_graphrag.sh` has no match;
host suite/lint passes.

## Test plan

Cover complete transcript, missing marker/qid/field, duplicate/conflicting qid, malformed/out-of-range
IDs, empty legitimate result, exact evidence aggregates, exact answer EM/F1, reader failure, denominator
stability, JSON/Markdown schema, and shell DONE gating. A real engine run is additional and must be
labeled x86 stand-in or GX10 accurately.

## Done criteria

- [ ] `graphrag-live` cannot print DONE before strict grading succeeds.
- [ ] Every manifest qid contributes to every reported denominator.
- [ ] JSON/Markdown identify corpus, engine-live scope, reader/evidence-only mode, and measured metrics.
- [ ] Engine-only docs do not imply a live multi-system baseline run.
- [ ] Focused/full tests, lint, shell syntax, negative control, and diff check pass.

## STOP conditions

- Generated SQL does not emit enough stable qid/ID/examined markers to grade; fix the emitter only
  after reporting the required scope expansion.
- Existing answer readers cannot consume contexts from live IDs deterministically.
- A proposed report drops failed questions from denominators.
- The only available run is off-GX10 and someone requests a GX10/full-corpus sign-off.

## Maintenance notes

The raw transcript is evidence and should remain alongside derived reports. Any new live metric must
be parsed strictly, tested with a missing-record negative case, and included before the DONE gate.
