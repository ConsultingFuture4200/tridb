# Plan 068: Harden the public wiki_reader (LLM-route DoS, error leak, response headers)

> **Executor instructions**: Follow step by step; run every verification. STOP conditions halt you.
> Update this plan's row in `advisor-plans/README.md` when done.
>
> **Drift check (run first)**: `git diff --stat a41b0c7..HEAD -- tools/wiki_reader.py`
> `wiki_reader.py` is large (3698 lines) and actively evolving — if it changed, re-locate each cited
> line by its surrounding code (the `grep` anchors below), not the absolute number.

## Status

- **Priority**: P1
- **Effort**: S–M
- **Risk**: LOW–MED (it backs a live public service — verify the read/search paths still work)
- **Depends on**: none
- **Category**: security
- **Planned at**: commit `a41b0c7`, 2026-07-15

## Why this matters

`tools/wiki_reader.py` serves the wiki corpus **publicly** through a Cloudflare tunnel
(`wiki.thumbox.io`). Three defensive gaps, framed as hardening (no exploit detail needed):

1. **Unbounded expensive LLM routes.** The `/ask`, `/enrich/<id>`, and `?narrate=1` GET routes each
   make a blocking local-LLM call (up to 180 s) on an unbounded-threaded server, with no auth, no
   concurrency ceiling, and no rate limit. A modest burst of anonymous requests exhausts
   CPU/GPU/threads and takes the demo offline.
2. **Internal error detail leaked to clients.** Both the GET and POST catch-alls send
   `html.escape(repr(e))` as the 500 body — exception types, file paths, and occasionally argument
   values go to unauthenticated clients (info disclosure / recon aid). It is escaped `text/plain`,
   so not XSS, but it exposes internals.
3. **No response-hardening headers.** `_send` sets only Content-Type/Length — no CSP,
   `X-Content-Type-Options`, `X-Frame-Options`/`frame-ancestors`, or `Referrer-Policy`. The
   article-body sanitizer is the *sole* XSS defense against publicly-editable upstream Wikipedia
   HTML; a single sanitizer gap becomes stored XSS with no CSP backstop, and missing frame headers
   allow clickjacking.

## Current state

- `tools/wiki_reader.py:44` — `from http.server import ..., ThreadingHTTPServer` (unbounded thread
  per connection); `:3633` — `httpd = ThreadingHTTPServer((host, port), ...)`.
- LLM-backed GET routes: `:3489` (`/ask`), `:3557` (`/enrich/<id>`), `:3590-3591`
  (`/path?...&narrate=1`); the blocking LLM calls at `:1102` (`timeout=120`), `:1588`/`:1847`
  (`timeout=180`) via `urllib.request.urlopen(OLLAMA_URL, ...)`.
- 500 handlers: `:3596` and `:3623` — `self._send(500, html.escape(repr(e)).encode(), "text/plain")`.
- `:3461-3467` — `_send` sets only `Content-Type` + `Content-Length`.
- There is already a token mechanism (`check_token`, `:3429-3439`) used on the mutating POST routes —
  reuse its shape for the LLM-route gate if you choose to require the token there.

Conventions: this file is plain-stdlib (no framework); match its handler style. There is an existing
`_send(code, body, ctype)` helper — route all responses through it.

## Steps

1. **Bound the LLM routes** (addresses gap 1):
   - Add a module-level `threading.Semaphore(N)` (e.g. `_LLM_SLOTS = threading.Semaphore(2)`) and
     wrap each LLM-backed call site (the `/ask`, `/enrich`, narrate handlers) in
     `if not _LLM_SLOTS.acquire(blocking=False): self._send(429, b"busy", "text/plain"); return`
     … `try: ... finally: _LLM_SLOTS.release()`. Pick N from the box's GPU capacity (2 is safe for a
     single dual-1070 host; make it an env var `WIKI_READER_LLM_SLOTS` defaulting to 2).
   - Drop the LLM `urlopen` timeouts from 120/180 s to something bounded (e.g. 30 s via an env
     `WIKI_READER_LLM_TIMEOUT`) so a stuck backend can't pin a slot indefinitely.
   - Optional (reviewer's call — note it, don't force): gate the LLM routes behind the existing
     token via `check_token`, since they are the costly ones. Keep the plain read/search routes
     open.

2. **Stop leaking exception detail** (gap 2): at `:3596` and `:3623`, replace
   `html.escape(repr(e))` with a static generic body and log the detail server-side:
   ```python
   import logging  # at top if not present
   ...
   except Exception as e:
       logging.exception("wiki_reader request failed")   # full detail to server log
       self._send(500, b"internal error", "text/plain")   # generic to client
   ```
   Do this for both the GET and POST catch-alls.

3. **Add response-hardening headers** (gap 3) in `_send` (`:3461`), applied to all responses:
   ```python
   self.send_header("X-Content-Type-Options", "nosniff")
   self.send_header("X-Frame-Options", "DENY")
   self.send_header("Referrer-Policy", "no-referrer")
   ```
   For CSP on HTML responses specifically: the page uses inline `<script>`/`<style>`, so a strict
   CSP needs a nonce or hash. **Minimal safe version for this plan**: add a CSP that locks image
   sources and framing without breaking the inline blocks — e.g.
   `default-src 'self'; img-src 'self' https://upload.wikimedia.org data:; frame-ancestors 'none'; object-src 'none'`
   and allow inline script/style only if the page needs it (`'unsafe-inline'` for `script-src`/`style-src`
   as an explicit, commented, documented interim until nonces are added — note this in the code
   comment as a known interim, since removing `'unsafe-inline'` is a larger change requiring nonce
   plumbing). Only send CSP on `text/html` responses (guard on `ctype`).

## Verification

This server has no automated test harness; verify by driving it locally (read-only, no public
exposure) and by static checks.

1. Static: `grep -c 'html.escape(repr(e))' tools/wiki_reader.py` == 0;
   `grep -c 'X-Frame-Options' tools/wiki_reader.py` ≥ 1; `grep -c 'Semaphore' tools/wiki_reader.py` ≥ 1.
2. `make lint` (ruff) → clean (the file is in the lint set).
3. Local smoke (do NOT expose publicly): start the reader on loopback per its `--help`/module
   docstring (find the run invocation in the file's `if __name__` block), then:
   - `curl -s -o /dev/null -w '%{http_code}\n' localhost:<port>/` → 200; the read/search/article
     routes still render (spot-check one `/article/<id>` and one search).
   - `curl -sD - localhost:<port>/ -o /dev/null | grep -i 'x-frame-options\|content-security-policy\|x-content-type-options'`
     → the new headers are present.
   - Trigger a 500 (e.g. a malformed route the handler will raise on) → body is `internal error`,
     not a `repr(...)`.
   - Fire 5 concurrent `/ask` requests → at most N run, the rest get 429 (or the token gate if you
     enabled it), and none hang past the reduced timeout.
   If you cannot determine a safe local run invocation, do the static checks + `make lint` and mark
   the live checks as "verify on the reader host" in your status note — do NOT expose the service to
   do the check.

## Done criteria

- No `repr(e)` in any 500 response (`grep` == 0).
- LLM routes are semaphore-bounded and time-bounded; over-limit returns 429.
- `_send` emits `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, and (on HTML) a CSP.
- `make lint` green; the read/search/article paths still return 200 with content.

## Out of scope / do NOT touch

- The `_HtmlSanitizer` whitelist (`:2117`) — it was reviewed as solid; CSP is the backstop, not a
  rewrite of the sanitizer.
- The overlay-mutation trust model (`accept_fact` bypass, page-embedded token) — that is a separate,
  larger finding (SECURITY-02 in the audit); if you want it, it needs its own plan (an operator
  secret not rendered into HTML + server-side re-validation of accepted facts). Do NOT attempt it
  here.
- The SQL/search paths (reviewed as parameterized/safe).

## STOP conditions

- If the page genuinely cannot function under any CSP without a large nonce-plumbing change, ship
  steps 1–2 + the three non-CSP headers, and record CSP as a follow-up with the reason — do NOT ship
  a CSP that breaks the live page.
- If reducing the LLM timeout breaks a legitimately slow model call in local testing, make the
  timeout env-configurable (as specified) and set a sane default; report the observed call duration.

## Maintenance note

New routes that call the LLM backend must go through the semaphore + timeout. New response types
should flow through `_send` so they inherit the hardening headers. The `accept_fact` overlay-trust
gap remains open (separate plan) — a reviewer should not consider the reader fully hardened until
that lands.
