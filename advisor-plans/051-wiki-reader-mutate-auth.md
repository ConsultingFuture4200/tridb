# Plan 051: Auth + body limits for wiki-reader mutating HTTP

> **Executor instructions**: Python `tools/wiki_reader.py` only. Update index when done.
>
> **Drift check**: `git diff --stat c216750..HEAD -- tools/wiki_reader.py docs/offline_wiki_reader_v0.1.0.md`

## Status
- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: security
- **Planned at**: commit `c216750`, 2026-07-09

## Why this matters

Wiki-reader is the only first-party HTTP surface. `POST /enrich/accept` and `/enrich/dismiss` mutate
`enrich_overlay.db` with **no auth**. Default bind is `127.0.0.1`, but `--host` is unrestricted.
`Content-Length` is unbounded (`rfile.read(length)`). Local tool threat model still needs fail-closed
defaults when exposed.

## Current state

```python
# tools/wiki_reader.py:2475-2487
def do_POST(self):
    length = int(self.headers.get("Content-Length", "0") or "0")
    body = json.loads(self.rfile.read(length) or b"{}") if length else {}
    if u.path == "/enrich/accept":
        self._json(reader.accept_fact(body))
```

- Serve: `--host` default `127.0.0.1` (`:2521-2522`)
- XSS posture mostly good (`html.escape` / client `esc`) — do not regress

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Lint/tests | `make test && make lint` | exit 0 |
| Optional unit | `pytest tests/test_wiki_reader.py -q` if added | pass |

## Scope

**In scope:** `tools/wiki_reader.py` serve/handler; short doc note in `docs/offline_wiki_reader_v0.1.0.md`; optional unit tests for auth helper.

**Out of scope:** full user accounts; TLS termination.

## Git workflow
- Branch: `advisor/051-wiki-reader-auth`
- Commit: `fix(wiki-reader): token + body cap on mutate POSTs (advisor 051)`

## Steps

### Step 1: Refuse non-loopback without explicit flag

If `host` not in `{127.0.0.1, ::1, localhost}` and `--allow-remote` not set → argparse error at serve start.

### Step 2: Mutating POST token

- Require header `X-TriDB-Token: <token>` (or `Authorization: Bearer`) matching serve-time token from
  env `WIKI_READER_TOKEN` or auto-generated printed once at startup.
- Reject 401 without token on `/enrich/accept` and `/enrich/dismiss` only (GET can stay open for local browse).

### Step 3: Body limits

- Cap Content-Length (e.g. 64 KiB); 413 if larger.
- Truncate/validate overlay string fields (value/snippet) symmetrically with property’s 300-char limit.

### Step 4: Docs

Document token + `--allow-remote` in offline reader doc and `--help`.

**Verify**: `make lint`; manual or unit test of cap/auth helpers.

## Test plan
- Prefer pure functions: `check_token`, `parse_body(max_len)` tested without binding a port.

## Done criteria
- [ ] Non-loopback requires `--allow-remote`
- [ ] Mutating POSTs require token
- [ ] Body size capped
- [ ] `make test`/`lint` green
- [ ] Index DONE

## STOP conditions
- UI hardcodes POSTs without a place to put token — update INDEX_HTML fetch to send token from a serve-injected meta tag (same origin only).

## Maintenance notes
- Do not commit default tokens into the repo.
