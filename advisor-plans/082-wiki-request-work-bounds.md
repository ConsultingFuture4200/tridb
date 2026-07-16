# Plan 082: Reject unbounded Wiki reader GET work parameters

> **Executor instructions**: Validate at the HTTP boundary before calling reader methods. Invalid
> supplied values return 400; do not silently coerce an attacker-provided value to a default. Skip
> the advisor index update.
>
> **Drift check (run first)**:
> `git diff --stat a780b46..HEAD -- tools/wiki_reader.py tests/test_wiki_reader.py docs/offline_wiki_reader_v0.1.0.md`

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: 081 (both edit the reader handler; land auth/integrity first)
- **Category**: security / perf / tests
- **Planned at**: commit `a780b46`, 2026-07-15

## Why this matters

The threaded public reader accepts arbitrary `hops`, semantic `pool`, tri-modal `seed`, and query
length. A single request can therefore amplify CPU/memory/graph/LLM work far beyond UI defaults.
Existing LLM concurrency controls limit parallelism, not per-request cost.

## Current state

- `tools/wiki_reader.py:3515-3527` parses `/ask?hops=` with raw `int`; invalid input silently becomes
  1 and valid huge/negative input is passed to `reader.ask`.
- Lines 3530-3547 parse `/search_semantic?pool=` and filter numbers; invalid values become zero and
  `pool` falls back to 150, but large valid values are unbounded.
- Lines 3549-3566 similarly pass arbitrary `/search_trimodal?seed=` values.
- `/search`, `/ask`, both semantic routes, and `/path` resolution accept unbounded decoded text.
- The service uses `ThreadingHTTPServer`; plan 068's `_LLM_SLOTS` protects LLM concurrency only.

## Fixed boundary contract

Use named module constants and one strict parser/helper:

| Field | Accepted | Default when omitted |
|---|---:|---:|
| query / path endpoint text | 0..512 Unicode code points (`/ask` requires nonempty) | route-specific |
| `hops` | integer 0..2 | 1 |
| semantic `pool` | integer 1..1000 | 150 |
| tri-modal `seed` | integer 1..200 | 40 |
| `min_indeg`, `min_len`, `max_len` | integer 0..10,000,000 | 0 |
| `cat` | 0..128 Unicode code points | empty |

Require `min_len <= max_len` only when `max_len > 0`. Preserve current boolean syntax for `expand`
and `narrate`, but reject values outside `0`/`1`. If committed UI/tests prove a listed maximum breaks
a legitimate workflow, STOP and propose a measured replacement rather than increasing it ad hoc.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused | `.venv/bin/pytest tests/test_wiki_reader.py -q` | all pass |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope**:
- `tools/wiki_reader.py`
- `tests/test_wiki_reader.py`
- `docs/offline_wiki_reader_v0.1.0.md` if it lists request parameters

**Out of scope**:
- Rate limiting, user accounts, reverse-proxy configuration, or changing LLM semaphore size.
- Changing search/ranking/traversal algorithms.
- Silently clipping out-of-range client values.

## Git workflow

Use assigned `dustin/dev-NNNN`; suggested commit:
`fix(wiki): bound public request work`.

## Steps

### Step 1: Add pure validation helpers and boundary tests

Implement helpers that distinguish omitted from malformed, parse one value only, enforce integer/
text ranges, and raise a typed request error with a safe message. Unit-test defaults, both accepted
boundaries, just-outside values, negative, non-integer, duplicate values, overlong decoded Unicode,
invalid booleans, and inconsistent length filters.

**Verify**: tests showing `hops=999999`, `pool=999999`, `seed=999999`, and a 513-character query are
accepted by current handlers or helpers and fail before the fix.

### Step 2: Apply validation before every expensive GET call

Parse and validate all parameters before acquiring `_LLM_SLOTS` or invoking reader methods. Apply
query bounds to `/search`, `/ask`, semantic/tri-modal search, and both `/path` labels. Apply category
and numeric bounds to relevant routes. Catch the typed validation error separately and return HTTP
400 with `text/plain`; do not route it through the generic 500 handler and do not log a traceback.

**Verify**: handler-level tests use a fake reader and assert invalid requests return 400 with zero
reader calls; boundary-valid requests call the expected method once with exact parsed values.

### Step 3: Keep UI defaults and docs aligned

Confirm browser controls stay within these limits. If docs expose parameters, list defaults/maxima
and state that invalid explicit values are rejected. Do not add instructional prose to the visible
app solely for these limits.

**Verify**: existing reader tests and a local UI smoke pass; `make test && make lint` pass.

## Test plan

Cover each route's default, min/max, malformed, duplicate, negative, too-large, and Unicode length;
assert no expensive method or semaphore acquisition occurs on 400. Include `/path` `from`/`to` and
the `min_len <= max_len` relation. Keep POST body tests from plan 081 green.

## Done criteria

- [ ] Every expensive GET argument has a named maximum enforced before reader work.
- [ ] Explicit malformed/out-of-range values return 400, never defaults or 500.
- [ ] Invalid requests invoke no reader method and acquire no LLM slot.
- [ ] Focused/full tests, lint, and diff checks pass.

## STOP conditions

- Plan 081 has not landed or materially restructures the handler; rebase and re-plan the seam.
- A documented legitimate workflow requires values beyond the fixed table.
- Correct enforcement requires changing reader algorithm semantics.

## Maintenance notes

New public routes must declare per-request text/cardinality/depth bounds and test “invalid means no
work.” Concurrency controls do not replace these cardinality limits.
