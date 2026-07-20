# Release checklist — v0.2.0

The exact maintainer steps, in order. Everything below was PREPARED and dry-run
verified by advisor plan 101 (images rebuilt + runtime smoke re-run on PG16 and PG17,
`release.yml` YAML-validated, version stamps lockstep-tested by
`tests/test_release_coherence.py`); only the trigger steps remain. Steps 3-5 are the
triggers — nothing before them publishes anything.

## 0. Preflight (repeatable, non-destructive)

```bash
make test && make lint
```

Verify: both exit 0. This includes the version-coherence gate
(`tests/test_release_coherence.py`: extension `.control` versions, release-image
label, release notes, this checklist, and `release.yml` all agree on `0.2.0`).

Optionally re-run the exact build+smoke the workflow will run:

```bash
make stock-release-smoke PG_MAJOR=16
make stock-release-smoke PG_MAJOR=17
```

Verify: each prints `RELEASE SMOKE PASS (tridb/postgres-trimodal:pgNN)`.

## 1. Review the release notes

```bash
${PAGER:-less} docs/releases/v0.2.0.md
```

Verify: you accept every claim — each number links to its in-repo evidence doc, and
the "Honest limits" section stays in. The GitHub Release body is this file, verbatim.

## 2. Make the repository public

At the v0.2.0 cut the repo was ALREADY public, so this step was a no-op — check
before running it:

```bash
gh repo edit ConsultingFuture4200/tridb --visibility public --accept-visibility-change-consequences
```

Verify:

```bash
gh repo view ConsultingFuture4200/tridb --json visibility
```

Note: going public before tagging matters — a Release and public GHCR images on a
private repo are not reachable by anyone else.

## 3. Push master

```bash
git push origin master
```

Verify: CI green on the pushed commit:

```bash
gh run watch $(gh run list --workflow=ci.yml --branch master --limit 1 --json databaseId --jq '.[0].databaseId')
```

## 4. Push the tag (this triggers the publish)

The tag MUST be `v0.2.0` — the workflow refuses any tag that does not equal
`v` + the extension `.control` version, so a typo fails fast instead of publishing
mislabeled images.

```bash
git tag -a v0.2.0 -m "TriDB v0.2.0" && git push origin v0.2.0
```

Verify: the release workflow started:

```bash
gh run list --workflow=release.yml --limit 1
```

## 5. Verify the workflow result

```bash
gh run watch $(gh run list --workflow=release.yml --limit 1 --json databaseId --jq '.[0].databaseId')
gh release view v0.2.0
```

Verify: both matrix legs (pg16, pg17) show the build, the `RELEASE SMOKE PASS`
runtime gate, and the GHCR push; the Release exists with the notes attached. Images:

```bash
docker pull ghcr.io/consultingfuture4200/tridb/postgres-trimodal:pg17-v0.2.0
```

## 6. Confirm the GHCR packages are public

OBSERVED at the v0.2.0 cut (2026-07-20): no flip was needed. The first workflow
push created `tridb/postgres-trimodal` already **public**, inheriting the public
repo's visibility. The pre-release guidance that GHCR always creates packages
private did not hold here.

Still verify rather than assume — if a future package does come out private, flip
it once in the web UI (package -> Package settings -> Change visibility -> Public,
github.com/users/ConsultingFuture4200/packages). Subsequent version pushes keep
whatever visibility the package has.

Verify (must succeed logged out / from a clean machine):

```bash
docker logout ghcr.io && docker pull ghcr.io/consultingfuture4200/tridb/postgres-trimodal:pg17
```

## 7. Announce

Point at the Release URL (`gh release view v0.2.0 --json url --jq .url`) and the
one-command try-it block at the top of `README.md`.

## First-tag risks (known, accepted — documented instead of guessed)

- **GHCR push is exercised for the first time on the first tag.** Locally we
  verified everything up to the push: both images rebuilt from this tree, both
  runtime smokes PASS, and `release.yml` YAML-parses with build/smoke commands
  byte-identical to CI's. The `docker login ghcr.io` + `docker push` with the
  workflow `GITHUB_TOKEN` (`packages: write`) cannot be verified without pushing.
  If the push leg fails, the run fails loudly BEFORE the GitHub Release job
  (`github-release` needs `build-smoke-publish`); fix and re-run the workflow from
  the Actions UI — the tag does not need to move.
- **Package visibility** (step 6) is a one-time manual flip; forgetting it means
  `docker pull` works for you and 404s for everyone else.
- **Dry-run mode exists:** `gh workflow run release.yml` (workflow_dispatch) runs
  build + smoke only — no push, no Release. Safe to run any time, including before
  step 2.
