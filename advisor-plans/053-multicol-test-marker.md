# Plan 053: Multicol fork harness must not grep bare 2000

> **Executor instructions**: Shell + SQL test harness only. Update index when done.
>
> **Drift check**: `git diff --stat c216750..HEAD -- scripts/fork_bug_multicol_test.sh test/`

## Status
- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: tests
- **Planned at**: commit `c216750`, 2026-07-09

## Why this matters

`scripts/fork_bug_multicol_test.sh` accepts success if the entire psql transcript matches bare
`2000` anywhere (`grep -qE '(^| )2000( |$)'`). Timing lines, row counts, or plan noise can false-PASS
a silent wrong-answer regression. This suite is in `AM_TESTS`.

## Current state

```bash
# scripts/fork_bug_multicol_test.sh ~52-56
echo "$OUT" | grep -qE '(^| )2000( |$)' \
  || fail "neither defense fired: ..."
```

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Syntax | `bash -n scripts/fork_bug_multicol_test.sh` | exit 0 |
| Engine | suite via `make graph-test` or direct script | PASS |

## Scope

**In scope:** multicol test script + its SQL input (find the SQL path referenced by the script).

**Out of scope:** fixing multicol TopK itself.

## Git workflow
- Branch: `advisor/053-multicol-marker`
- Commit: `test(engine): labeled marker for multicol unordered count (advisor 053)`

## Steps

### Step 1: Emit a labeled marker from SQL

In the multicol SQL fixture, print a unique tag, e.g.:

```sql
\echo unordered_count|2000
```

or `SELECT 'unordered_count|' || count(*) ...`.

### Step 2: Grep the tag

Replace bare `2000` check with `grep -q 'unordered_count|2000'` (exact).

**Verify**: script still passes on healthy engine; intentionally wrong expected tag fails.

## Test plan
- Engine run once after change.

## Done criteria
- [ ] No bare-`2000` success criterion
- [ ] Engine suite green
- [ ] Index DONE

## STOP conditions
- Suite structure changed and no longer uses count 2000 — re-read script and match its real oracle.

## Maintenance notes
- Same pattern may exist elsewhere — only fix this harness in this plan (`rg '2000' scripts/*test*` optional note in maintenance, no drive-by).
