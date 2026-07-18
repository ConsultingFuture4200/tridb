# Plan 101: Release-cut preparation — v0.2.0, publishable images, public front door

> **Executor instructions**: PREPARE everything; PULL NO TRIGGERS. The maintainer flips the repo
> public, pushes the tag, and runs the publish workflow — your deliverable is that those are
> each ONE action with nothing left to decide. Runs LAST (after 098/099/100 merge). Skip the
> advisor index update.
>
> **Drift check (run first)**:
> `git diff --stat <base>..HEAD` — read the final state of README/INSTALL/ci.yml/Makefile after
> the 097-100 merges before editing anything.

## Status

- **Priority**: P1 (spike→product item 3)
- **Effort**: S–M
- **Risk**: LOW (docs/CI/packaging only)
- **Depends on**: 097, 098, 099, 100 (all merged)
- **Planned at**: 2026-07-18

## Deliverables

1. **Release notes** `docs/releases/v0.2.0.md`: the un-fork story (stock PG16/17 + pgvector),
   TR-1 bounded graph leg (ADR-0020), PPR-graded default with both gate tables cited (ADR-0021),
   the MCP agent-memory surface, backup/restore + upgrade-path + single-writer contracts, and
   the honest-limits section (single-writer, GX10-gated fork claims, what benchmarks were run at
   what scale). Herald-style: readable by a stranger, no internal codenames, every number linked
   to its in-repo evidence doc.
2. **Publish workflow** `.github/workflows/release.yml`: on tag `v*`, build
   `tridb/postgres-trimodal:pg16|pg17` release images, run the release smoke against BOTH, and
   push to GHCR (`ghcr.io/<owner>/...`) using the default `GITHUB_TOKEN` with `packages: write`
   permission — no new secrets. Also attach the release notes to a GitHub Release. The workflow
   must be exercised as far as possible locally (image build + smoke re-run; `act` NOT required
   — YAML-parse + a dispatch-mode dry-run job if feasible, else document exactly what runs on
   first tag).
3. **Front door pass** on README: the first screen answers "what is this / one command to try it
   (docker run + MCP demo) / what's proven at what scale" — reusing existing corrected copy, not
   rewriting claims. Add LICENSE check: confirm the repo HAS a license file consistent with the
   pgvector-ecosystem posture (memory: MIT-compatible intent); if MISSING, STOP and report —
   license choice is the maintainer's.
4. **Release checklist** `docs/releases/CHECKLIST.md`: the exact maintainer steps in order
   (review notes → make repo public → push tag → verify workflow → announce), each one command,
   with the verification for each.
5. **Version stamp coherence**: extension versions (0.2.0 post-plan-100), image labels, release
   notes, and the tag name all agree; a tiny host test greps them into lockstep.

## Out of scope

- Making the repo public, pushing tags, publishing images (maintainer triggers).
- Any engine/SQL change; any new claims (release notes cite only existing evidence docs).
- Site/wiki reader deploys.

## Verification

- Both release images rebuilt from the final tree + smoke PASS (re-run, not assumed).
- `release.yml` YAML-parses; its smoke steps are byte-consistent with `scripts/pg17_release_smoke.sh` usage.
- Version-coherence test green; `make test && make lint`.
- The checklist executed in dry-run form up to (not including) the trigger steps.

## STOP conditions

- No LICENSE file (report; do not choose one).
- Release notes would need a claim no evidence doc backs (report the gap instead of writing it).
- GHCR push requires configuration you cannot verify locally beyond YAML review — document the
  first-tag risk explicitly in the checklist rather than guessing.

## Git workflow

Branch `advisor/101-release-prep`. Commits: `docs(release): v0.2.0 notes + checklist`,
`ci(release): tag-triggered image publish`, `docs: front-door pass`.

REPORT FORMAT: STATUS / STEPS+verifications / FILES CHANGED / NOTES (incl. anything the
maintainer must decide before tagging) / WORKTREE+commits.
