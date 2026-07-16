# Plan 080: Attribute public Wiki-scale claims to the engine that produced them

> **Executor instructions**: This is a factual correction, not a visual redesign. Keep the reader
> usable and test claims against the evidence docs. Skip the advisor index update.
>
> **Drift check (run first)**:
> `git diff --stat a780b46..HEAD -- site/index.html tools/wiki_reader.py tests/ docs/benchmark_wiki_scale_h2h_v0.2.0.md docs/benchmark_tjs_open_ref_v0.1.0.md docs/offline_wiki_reader_v0.1.0.md`

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: docs / tests
- **Planned at**: commit `a780b46`, 2026-07-15

## Why this matters

The public portal presents reader-side full-corpus behavior and a small-reference-corpus work ratio as
native TriDB results at 6.9 million entities. The underlying reader is valuable, but the attribution
is false and undermines benchmark credibility. Every visible metric must name its corpus scale and
execution engine.

## Current state

- `site/index.html:116,155,186,193,206` contains claims including native/fused behavior at 6.9M and
  a 0.71% graph-work figure.
- `tools/wiki_reader.py` embeds a second `LANDING_HTML` copy around lines 2285-2500 with the same
  claims. Both public surfaces must remain factually aligned.
- `tools/wiki_reader.py:7-17` describes the implementation as SQLite metadata, NumPy CSR topology,
  and cuVS CAGRA vectors: this is a reader-side index stack, not the Postgres-native operator.
- `docs/benchmark_wiki_scale_h2h_v0.2.0.md:91-108` records graph-inclusive native engine evidence at
  200K and vector evidence at 1M; a full 6.9M native graph-inclusive run was not performed.
- `docs/benchmark_tjs_open_ref_v0.1.0.md` derives 0.71% from a 1,490-paragraph host reference corpus,
  not the full Wiki corpus.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused | `.venv/bin/pytest tests/test_wiki_reader_claims.py -q` | all pass |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope**:
- `site/index.html`
- `tools/wiki_reader.py` (`LANDING_HTML` copy only)
- `tests/test_wiki_reader_claims.py` (create)
- `docs/offline_wiki_reader_v0.1.0.md` only for a short provenance addendum if needed

**Out of scope**:
- Reader algorithms, APIs, styling, or deployment.
- Re-running benchmarks or inventing replacement numbers.
- Weakening accurately scoped native engine results in evidence docs.

## Git workflow

Use assigned `dustin/dev-NNNN`; suggested commit:
`docs(wiki): attribute scale claims accurately`.

## Steps

### Step 1: Encode claim guardrails

Create a host test that reads both HTML copies. Assert prohibited combinations are absent (for
example “native” or “TriDB fusion” attached to 6.9M, and 0.71% without its HotpotQA/reference
qualifier). Assert required provenance phrases identify the full-corpus experience as the offline
reader using reader-side indexes, and identify native engine evidence only at measured scales.
Compare normalized key claim strings between the two copies so one cannot drift silently.

**Verify**: the test fails against the current copy.

### Step 2: Rewrite visible claims with corpus and engine labels

Keep 6.9M as the inspected corpus size, but describe search/path operations as offline-reader
execution over SQLite/NumPy CSR/cuVS CAGRA. Label native graph-inclusive evidence as 200K and native
vector evidence as 1M where those facts are useful. Either remove 0.71% from the landing experience
or label it explicitly as the 1,490-paragraph host reference result. Do not imply native shortest
path when NumPy CSR computed it.

Apply the same factual wording to `site/index.html` and embedded `LANDING_HTML`.

**Verify**: focused guardrail test passes; manually render both surfaces at desktop/mobile and check
text does not overflow existing containers.

### Step 3: Record provenance maintenance guidance

If the offline-reader doc currently conflates these layers, append a short table mapping claim to
corpus, engine, and evidence document. Do not duplicate all benchmark prose.

**Verify**: every retained numeric performance/work claim has an adjacent engine/corpus qualifier or
is removed.

## Test plan

Static tests cover prohibited pairings, required provenance, and parity between standalone/embedded
HTML. Run host suite/lint. Perform browser screenshots only to catch copy-induced overflow; no design
changes are expected.

## Done criteria

- [ ] No public copy attributes the 6.9M reader run to native TriDB/Postgres execution.
- [ ] The 0.71% number is removed or labeled with its 1,490-paragraph host-reference provenance.
- [ ] Standalone and embedded public copy agree on engine/corpus attribution.
- [ ] Focused/full tests, lint, diff check, and desktop/mobile text inspection pass.

## STOP conditions

- Evidence docs now contain a completed 6.9M native graph-inclusive run; stop and reconcile against
  the new artifact rather than using this plan's scale table.
- Required copy changes would need a broad page redesign.
- A retained number lacks a committed evidence source.

## Maintenance notes

Future public metrics should carry three labels at authoring time: corpus, execution engine, and
artifact path. Keep reader experience claims distinct from native-engine benchmark claims.
