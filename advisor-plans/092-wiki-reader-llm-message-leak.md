# Plan 092: Stop leaking exception repr and backend URL in the public reader's LLM-unavailable message

> **Executor instructions**: Tiny, surgical. The public 200-path answer text must not carry
> `repr(e)` or the backend URL; operator diagnostics go to the server log. Skip the advisor index
> update.
>
> **Drift check (run first)**:
> `git diff --stat a780b46..HEAD -- tools/wiki_reader.py tests/test_wiki_reader.py`
> Plans 081 and 082 restructure the same file — land this AFTER both to avoid conflicts.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: 081, 082 (same-file serialization)
- **Category**: security (info disclosure)
- **Planned at**: commit `a780b46`, 2026-07-16

## Why this matters

Plan 068 removed `repr` leaks from the 500 handler, but the LLM-unavailable fallback returns a 200
whose *body* embeds `{e!r}` plus `OLLAMA_URL` and the model name. On the public tunnel that
discloses internal topology (backend host/port, model) and raw exception detail to any visitor —
the exact leak class 068 closed elsewhere.

## Current state (verified)

`tools/wiki_reader.py:1599-1603` (inside the LLM call helper):

```python
except Exception as e:  # surface the failure honestly rather than fabricate
    return (
        f"[LLM unavailable: {e!r}. Is ollama serving '{ASK_MODEL}' at "
        f"{OLLAMA_URL}? The retrieved sources below are still valid.]"
    )
```

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused | `.venv/bin/pytest tests/test_wiki_reader.py -q` | all pass |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope**: `tools/wiki_reader.py` (this except block + a log line), `tests/test_wiki_reader.py`.

**Out of scope**: any other route/handler behavior; 068/081/082 surfaces; changing LLM call logic.

## Git workflow

Use assigned `dustin/dev-NNNN`. Suggested commit: `fix(wiki): scrub llm fallback message`.

## Steps

### Step 1: Test the leak

Add a test that forces the exception path (monkeypatch `urllib.request.urlopen` to raise) and
asserts the returned string contains neither the exception repr fragment nor the `OLLAMA_URL`
value nor the model name, while still telling the user the answer is unavailable and sources are
valid.

**Verify (negative control)**: fails against current code.

### Step 2: Scrub the message, log the detail

Return a generic user-facing message (e.g. "[Answer generation is temporarily unavailable. The
retrieved sources below are still valid.]") and emit the full `repr(e)`/URL detail through the
module's existing server-side logging pattern (match how 068's opaque-500 path logs).

**Verify**: focused test passes; `make test && make lint && git diff --check` green.

## Test plan

Exception-path scrub test + existing reader suites unchanged.

## Done criteria

- [ ] No exception repr, backend URL, or model name in any 200-path body.
- [ ] Operator detail still reaches the server log.
- [ ] Focused/full tests + lint green; only in-scope files changed.

## STOP conditions

- Plans 081/082 not yet merged (same-file churn).
- The block has moved/changed materially since `a780b46` — re-locate by content, re-verify the
  leak still exists; if 081/082 already fixed it, report NO-CHANGE-NEEDED.

## Maintenance notes

Public-body messages never carry `repr`, URLs, hostnames, or model identifiers; those go to logs.
Grep for `!r` in handler-reachable strings when touching the reader.
