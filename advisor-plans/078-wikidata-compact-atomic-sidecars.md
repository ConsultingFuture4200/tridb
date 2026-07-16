# Plan 078: Publish Wikidata compact sidecars atomically and verify their identity

> **Executor instructions**: Implement with same-directory temporary files and fail-closed metadata.
> Run the injected-failure tests. Skip the advisor index update.
>
> **Drift check (run first)**:
> `git diff --stat a780b46..HEAD -- tools/wikidata_compact.py tools/wikidata_ingest.py tests/test_wikidata_compact.py docs/`

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug / tests
- **Planned at**: commit `a780b46`, 2026-07-15

## Why this matters

The compactor writes directly to the final gzip sidecar and writes metadata only afterward. A crash
can leave a valid gzip prefix or stale data paired with old metadata, and ingest does not verify the
sidecar's content identity. Such partial corpus input can silently contaminate every downstream
benchmark.

## Current state

- `tools/wikidata_compact.py:198-232` opens final `compact.tsv.gz` with `wb` and appends gzip-member
  blobs directly. Metadata is written only at lines 233-252.
- `tools/wikidata_ingest.py:481-485` reads concatenated gzip members, so a valid prefix can look like
  a complete file.
- `tools/wikidata_ingest.py:825-849` permits missing metadata and only compares language, limit, and
  source dump size; it does not hash or size the compact sidecar.
- `tests/test_wikidata_compact.py` already covers CLI/metadata behavior and is the focused test home.

## Target publication protocol

Write data and metadata to unique same-directory temporary files. Compute compact byte length and
SHA-256 over the exact compressed bytes being published. Flush and `fsync` each temporary file,
atomically `os.replace` data first and metadata second, then fsync the parent directory where
supported. A crash between replaces is safe because old metadata will not match new data. Ingest
requires metadata and exact size/digest agreement.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused | `.venv/bin/pytest tests/test_wikidata_compact.py -q` | all pass |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope**:
- `tools/wikidata_compact.py`
- `tools/wikidata_ingest.py`
- `tests/test_wikidata_compact.py`
- Existing Wikidata pipeline docs only if they describe sidecar reuse/validation

**Out of scope**:
- Changing compact row format, shard selection, or corpus semantics.
- Regenerating committed data/benchmark artifacts.
- Replacing multiprocessing architecture.

## Git workflow

Use assigned `dustin/dev-NNNN`; suggested commit:
`fix(wikidata): publish compact corpus atomically`.

## Steps

### Step 1: Add deterministic failure-injection tests

Refactor only enough to expose a writer/publication seam. Test failure during worker/member copy and
between the data/meta replacements. Pre-create known-good final data+metadata and assert a failed run
never produces a pair ingest accepts unless it is wholly old or wholly new. Assert temporary files
are cleaned. Do not depend on nondeterministic process termination.

**Verify**: the direct-write failure case fails against current code.

### Step 2: Stream to a temporary data file with an incremental digest

Wrap the temporary binary sink so every compressed byte updates SHA-256 and byte count while member
blobs are copied. Preserve multi-member gzip output. Flush/fsync before replacement and close all
worker resources on error. Use unique temp names in the destination directory.

**Verify**: successful output decompresses to the same rows/count/order as before; metadata digest
matches `sha256sum` and size matches `stat`.

### Step 3: Publish self-identifying metadata atomically

Include format/version, compact byte size, compact SHA-256, existing source identity fields, and row
counts in metadata. Write/fsync its temp file, replace data then metadata, and fsync the directory.
Clean only this run's temp paths in `finally`; never delete a pre-existing final file on failure.

**Verify**: injected failures preserve a fail-closed state and leave no temp files.

### Step 4: Require and verify metadata during ingest

Reject a compact sidecar when metadata is absent, malformed, stale, size-mismatched, or digest-
mismatched. Validate before consuming rows. Error text should name the failed identity check and tell
the operator to re-run compaction, without dumping corpus content.

**Verify**: tests reject truncated, appended, byte-tampered, missing-meta, and stale-meta sidecars;
the valid fixture passes.

## Test plan

Cover success, worker failure, publication failure, preservation of old finals, temp cleanup,
missing/malformed metadata, size mismatch, hash mismatch, truncation with a valid gzip prefix, and
successful reuse. Run focused tests, then full host tests/lint.

## Done criteria

- [ ] Final data is never opened for in-place writing.
- [ ] Metadata contains and ingest verifies compressed-byte size plus SHA-256.
- [ ] Every injected failure leaves either a valid old pair, valid new pair, or a rejected pair.
- [ ] Existing compact row content/order remains unchanged.
- [ ] Focused/full tests, lint, and `git diff --check` pass.

## STOP conditions

- Destination filesystem does not support atomic same-filesystem rename; report the deployment
  constraint rather than falling back to direct writes.
- The current format has an external compatibility contract that forbids adding metadata keys.
- Failure tests require modifying real corpus artifacts.

## Maintenance notes

Metadata is the commit record. Add future sidecar-affecting options to its identity fields, and keep
validation fail-closed even if hashing adds a sequential read cost.
