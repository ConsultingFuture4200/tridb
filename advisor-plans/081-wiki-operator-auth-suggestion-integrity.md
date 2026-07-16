# Plan 081: Keep Wiki operator credentials secret and revalidate accepted facts server-side

> **Executor instructions**: Treat all browser fields as attacker-controlled. Never commit or render
> an operator secret. Preserve zero-config loopback use, but require explicit credentials for remote
> mode. Skip the advisor index update.
>
> **Drift check (run first)**:
> `git diff --stat a780b46..HEAD -- tools/wiki_reader.py tests/test_wiki_reader.py docs/offline_wiki_reader_v0.1.0.md .env.example`

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: 068 (already merged public-route hardening)
- **Category**: security / bug / tests
- **Planned at**: commit `a780b46`, 2026-07-15

## Why this matters

Remote clients receive the same mutation token they must present, so the token is not authorization.
The accept endpoint also persists client-supplied property/value/source fields without repeating the
grounding check that created the suggestion. A visitor who can load the page can forge overlay facts
that later look like accepted knowledge.

## Current state

- `tools/wiki_reader.py:3459-3463` embeds the mutation token in page metadata.
- POST handling at lines 3656-3664 checks the submitted token against that embedded/server value.
- `accept_fact` at lines 1993-2023 persists client-provided property, value, source title, and snippet.
- Enrichment at lines 1922-1988 already derives suggestions from source text and calls
  `_snippet_grounded`; this validation is not repeated at acceptance.
- Browser code around lines 2956-2963 sends the full suggestion payload back from `_enrichData`.
- Remote bind is explicitly supported around lines 3706-3725.
- `tests/test_wiki_reader.py` covers token/body helpers but has no end-to-end authorization or
  suggestion-tampering coverage.

## Target security contract

- Loopback-only mode may generate an ephemeral session token and render it for local single-user
  ergonomics, with that limitation documented.
- Any non-loopback bind requires `WIKI_READER_OPERATOR_TOKEN` supplied out of band. Startup fails if
  absent/weak; HTML and API responses never contain it. The browser asks the operator once and keeps
  it in `sessionStorage` (or equivalent non-persistent session state), sending it in a header.
- Enrichment stores a bounded, expiring server-side canonical suggestion keyed by opaque random ID.
  Accept/dismiss requests send only subject ID + suggestion ID. Acceptance reloads the source,
  repeats grounding, derives all persisted fields server-side, then consumes the ID exactly once.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused | `.venv/bin/pytest tests/test_wiki_reader.py -q` | all pass |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope**:
- `tools/wiki_reader.py`
- `tests/test_wiki_reader.py`
- `docs/offline_wiki_reader_v0.1.0.md` (append security/operation addendum)
- `.env.example` (variable name and placeholder only)

**Out of scope**:
- Multi-user accounts, OAuth, Internet-facing identity management, or a general ACL system.
- Trusting an HMAC supplied with client-authored fields; canonical fields remain server-owned.
- Changing search/graph/vector algorithms.
- Committing any token value.

## Git workflow

Use assigned `dustin/dev-NNNN`; suggested commit:
`fix(wiki): secure operator mutations`.

## Steps

### Step 1: Extract testable mode and authorization helpers

Represent bind mode explicitly. Add pure tests for loopback versus non-loopback addresses, remote
startup with missing token, constant-time token comparison, unauthorized POST, and generated HTML.
Assert remote HTML does not contain the configured secret anywhere. Keep existing hardening headers
from plan 068.

**Verify**: current remote-HTML secrecy test fails.

### Step 2: Separate local session convenience from remote operator authentication

For loopback, preserve generated-token behavior but mark it local-only. For non-loopback, require a
strong nonempty `WIKI_READER_OPERATOR_TOKEN` from environment/startup configuration and never pass it
to page rendering. Add a small operator-token prompt/control only when a mutation is attempted; keep
the value in session storage and send it in a dedicated header. Do not place it in URLs/logs.

**Verify**: remote startup without token exits with a clear message; configured remote token is
accepted but absent from HTML/log fixtures; wrong/missing tokens return 403.

### Step 3: Make suggestion state canonical and bounded on the server

When enrichment emits suggestions, register each canonical payload in a thread-safe pending store
using a cryptographically random opaque ID. Record subject/source IDs, canonical property/value,
source title locator, grounded snippet evidence, creation/expiry time, and one-use state. Bound both
entry count and TTL; evict deterministically under the existing threaded server. Return only the ID
and display-safe suggestion data to the client.

**Verify**: tests cover capacity eviction, expiration, subject mismatch, unknown ID, and concurrent
single-use acceptance.

### Step 4: Revalidate at the mutation boundary

Accept requests carry subject ID + suggestion ID only. Load the canonical entry, reload the current
source article through existing reader APIs, derive source title server-side, and run
`_snippet_grounded` again against current content. Validate subject/source/property/value types and
allowed property vocabulary before persistence. Consume the ID atomically only after successful
write; reject expired, altered-source, mismatched, replayed, or ungrounded suggestions with 400/409
as appropriate. Dismiss consumes by ID without accepting client fact fields.

**Verify**: a client attempt to alter property/value/source cannot affect persisted data; changed
source text causes rejection; valid canonical suggestion persists once.

### Step 5: Document secure operation

Add the env key name with an empty placeholder to `.env.example`. Append docs explaining local-only
generated token behavior, remote token provisioning, rotation/restart, no URL transport, pending
suggestion TTL, and that this remains a single-operator service rather than public multi-user auth.

**Verify**: `git grep -n 'WIKI_READER_OPERATOR_TOKEN=' -- ':!*.example'` finds no committed value.

## Test plan

Cover local/remote mode, missing/weak/wrong/correct token, response/HTML/log secret absence, unknown/
expired/mismatched/replayed suggestion IDs, bounded eviction, concurrent one-time acceptance, client
field tampering, source-content change, grounding rejection, and valid persistence. Prefer pure
handler/service seams; avoid flaky socket tests unless existing patterns support them.

## Done criteria

- [ ] A remote mutation credential is never rendered, logged, or put in a URL.
- [ ] Non-loopback startup fails without an explicit operator token.
- [ ] Persisted fact fields come only from server-held canonical suggestion state.
- [ ] Acceptance repeats grounding against current source content and is one-use.
- [ ] Pending state is thread-safe, TTL-bound, and count-bound.
- [ ] Focused/full tests, lint, secret grep, and diff checks pass.

## STOP conditions

- Deployment requires anonymous public mutation; that conflicts with the security goal and needs a
  product decision.
- The reader cannot reload source content at acceptance time; do not persist without revalidation.
- A proposed implementation sends the remote token to every page visitor.
- Correct single-use behavior requires replacing the storage architecture; report the blocker.

## Maintenance notes

This is single-operator authorization, not identity. Any future multi-user deployment needs per-user
auth/audit design. Keep pending suggestions ephemeral and never treat browser-returned fields as
canonical.
