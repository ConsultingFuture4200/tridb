# Plan 054: make clean must not wipe data/

> **Executor instructions**: Makefile only. Update index when done.
>
> **Drift check**: `git diff --stat c216750..HEAD -- Makefile README.md CONTRIBUTING.md`

## Status
- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: dx
- **Planned at**: commit `c216750`, 2026-07-09

## Why this matters

```makefile
# Makefile ~385-386
clean:
	rm -rf data/ bench/out/ .pytest_cache/
```

`data/` holds seed corpora, public ANN sets, HotpotQA, and multi-GB wiki artifacts. One typo costs
hours of re-fetch/re-embed. Caches and bench outputs should clean; corpora should not.

## Current state

- `.PHONY` includes `clean`
- No `clean-data` target today

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Dry | `make -n clean` | does not list `rm -rf data/` |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope:** `Makefile` `clean` / new `clean-data`; one-line README or CONTRIBUTING note if clean is documented.

**Out of scope:** deleting wiki from developer machines; changing gitignore.

## Git workflow
- Branch: `advisor/054-make-clean`
- Commit: `chore(make): preserve data/ on make clean (advisor 054)`

## Steps

### Step 1: Scope clean

```makefile
clean:
	rm -rf bench/out/ .pytest_cache/
	# optional: __pycache__ find — keep light

clean-data:
	@echo "This deletes data/ (seed, ANN, wiki). Ctrl-C within 3s to abort." 
	@sleep 3
	rm -rf data/
```

Or require `CONFIRM=1` instead of sleep — pick one fail-safe.

**Verify**: `make -n clean` has no `data/`; `make -n clean-data` does.

### Step 2: Docs

If README mentions `make clean`, update.

## Test plan
- `make -n` only; do not run clean-data in CI.

## Done criteria
- [ ] `make clean` does not remove `data/`
- [ ] Explicit corpus wipe target exists and is hard to invoke by accident
- [ ] Index DONE

## STOP conditions
- Something in CI relies on `make clean` wiping data — unlikely; if found, fix CI not restore data wipe.

## Maintenance notes
- Reviewer: ensure `bench/out/` still cleaned (artifacts).
