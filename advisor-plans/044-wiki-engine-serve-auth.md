# Plan 044: Stop publishing trust-auth Postgres on host ports

> **Executor instructions**: Shell + harness connection strings. Update index when done.
>
> **Drift check**: `git diff --stat c216750..HEAD -- scripts/wiki_engine_serve.sh scripts/wiki_engine_load.sh bench/wiki_h2h.py bench/wiki_fusion.py bench/wiki_consistency.py SECURITY.md`

## Status
- **Priority**: P1
- **Effort**: M
- **Risk**: MED (breaks harnesses that assume trust on published port)
- **Depends on**: none
- **Category**: security
- **Planned at**: commit `c216750`, 2026-07-09

## Why this matters

`scripts/wiki_engine_serve.sh` publishes Postgres to the host with **`initdb -A trust`**,
`host all all 0.0.0.0/0 trust`, and `docker -p ${PORT}:5432` (all interfaces). Any client that can
reach the port gets **superuser SQL with no password** — full engine compromise on multi-user or
network-reachable hosts. This is stronger risk than “weak default password.”

## Current state

```bash
# scripts/wiki_engine_serve.sh:18,24-36 (approx)
chmod 777 "$OUT"
docker run ... -p "${PORT}:5432" ...
  initdb -A trust
  echo "host all all 0.0.0.0/0 trust" >> pg_hba.conf
  listen_addresses=*
```

- Harnesses connect with env defaults (`WH_PGPASSWORD`, etc.) — often unused under trust.
- SECURITY.md already notes image entrypoint superuser posture; this script actively publishes it.
- Do **not** quote any real secrets in commits; generate ephemeral passwords at serve time.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Syntax | `bash -n scripts/wiki_engine_serve.sh` | exit 0 |
| Host tests | `make test && make lint` | exit 0 |

## Scope

**In scope:**
- `scripts/wiki_engine_serve.sh` (and `wiki_engine_load.sh` only if it also publishes ports the same way)
- Harness env docs: `bench/wiki_h2h.py` / fusion / consistency connection defaults if they must read a password file
- `SECURITY.md` short note + `.env.example` keys for wiki engine password/path
- Prefer bind `127.0.0.1:${PORT}:5432` by default

**Out of scope:** baseline docker-compose passwords (local fixtures, separate); CI throwaway containers that do not publish ports.

## Git workflow
- Branch: `advisor/044-serve-auth`
- Commit: `fix(security): passworded loopback wiki engine serve (advisor 044)`

## Steps

### Step 1: Default bind loopback

Change publish to `-p "127.0.0.1:${PORT}:5432"` unless env `TRIDB_SERVE_BIND=0.0.0.0` (or similar) is set **explicitly**.

**Verify**: `rg -n '127.0.0.1|0.0.0.0' scripts/wiki_engine_serve.sh` shows loopback default.

### Step 2: Password auth for TCP

1. Generate a random password at start of serve (e.g. `openssl rand -hex 16`).
2. Write to `$OUT/pg_password` with mode `0600` on the host.
3. `initdb` with password for postgres user **or** `ALTER USER` after start via local socket trust only.
4. `pg_hba.conf`: `host all all 127.0.0.1/32 scram-sha-256` (or md5 if PG 13 image lacks scram client — prefer scram).
5. Keep **unix socket / docker exec** path usable without password for load.sql inside the container.

**Verify**: from host, `psql` without password fails; with password from file succeeds. Document in script header.

### Step 3: Wire harnesses

- Document `WH_PGPASSWORD` / `WC_PG_PASS` / fusion cfg reading `$OUT/pg_password` or env.
- Update any hard-coded trust assumptions in comments.

**Verify**: `make test && make lint`; `bash -n` on scripts.

### Step 4: Drop world-writable out dir if possible

Replace `chmod 777 "$OUT"` with group-writable or matching container UID (`--user`) so load.done cannot be planted by arbitrary local users. If hard on this image, document residual and use `770` + shared group as minimum.

**Verify**: script no longer uses `chmod 777` without comment, or uses safer mode.

## Test plan
- Manual smoke on Docker host (if available): serve tiny prep, connect with password.
- Host unit tests unchanged unless connection helpers gain validation (identifier allowlist can wait for other plans).

## Done criteria
- [ ] Default publish is loopback-only
- [ ] TCP requires password; password file mode 0600
- [ ] No `host all all 0.0.0.0/0 trust` in the default path
- [ ] SECURITY.md / script header document the new contract
- [ ] `bash -n` + `make test`/`lint` green
- [ ] Index DONE

## STOP conditions
- Timer-parity with multi-store baseline **requires** binding all interfaces on a shared lab LAN — implement opt-in flag, never default open trust.
- Image cannot set password non-interactively — report and keep loopback + trust **only on 127.0.0.1** as interim (still better than 0.0.0.0/0 trust).

## Maintenance notes
- Reviewer: ensure load.sql path inside container still works.
- Do not commit generated password files.
