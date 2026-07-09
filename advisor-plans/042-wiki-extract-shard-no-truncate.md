# Plan 042: Fix wiki extract shard rotate — no truncate on revisit

> **Executor instructions**: Python tooling; host-verifiable. Update index when done.
>
> **Drift check**: `git diff --stat c216750..HEAD -- tools/wiki_extract.py tools/wiki_extract_html.py tests/test_wiki_extract.py tools/wiki_engine_load.py`

## Status
- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: bug / correctness
- **Planned at**: commit `c216750`, 2026-07-09

## Why this matters

Full enwiki extraction already lost ~4% of articles/edges when shard writers reopened existing shard
files in **truncate** mode and re-appended manifest descriptors. Root cause is still in
`tools/wiki_extract.py` `_ShardWriter._rotate`: `open("w")` + always `append` a new descriptor. Any
non-monotonic `article["id"] // shard_size` (duplicate normalized titles → same id later) **wipes**
prior content for that shard while the manifest over-states paths/rows.

## Current state

```python
# tools/wiki_extract.py:296-307
def _rotate(self, shard_idx: int) -> None:
    self._close_current()
    self._idx = shard_idx
    ...
    self._af = ap.open("w", encoding="utf-8")  # TRUNCATES
    ...
    self.articles_shards.append({"path": ap.name, "rows": 0})  # re-appends descriptor
```

```python
# tools/wiki_extract.py:325-327
shard_idx = article["id"] // self.shard_size
if shard_idx != self._idx:
    self._rotate(shard_idx)
```

- Loader already de-dupes paths as a band-aid: `tools/wiki_engine_load.py` comments ~93-97.
- Manifest verify: `tools/wiki_manifest_verify.py`.
- Tests: `tests/test_wiki_extract.py` — extend; model after existing extract fixtures.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Extract unit tests | `pytest tests/test_wiki_extract.py -q` | all pass |
| Full | `make test && make lint` | exit 0 |

## Scope

**In scope:**
- `tools/wiki_extract.py` (`_ShardWriter`)
- `tools/wiki_extract_html.py` if it copies the same rotate pattern (grep `_rotate` / `open("w")`)
- `tests/test_wiki_extract.py` (new cases)
- Brief note in extract module docstring / STATUS only if a one-line pointer is needed

**Out of scope:** re-extracting production enwiki; rewriting manifests on disk; Neo4j loaders.

## Git workflow
- Branch: `advisor/042-extract-shard-rotate`
- Commit: `fix(wiki): never truncate extract shards on rotate (advisor 042)`

## Steps

### Step 1: Open-once / append semantics

Change `_ShardWriter` so each `shard_idx` is opened **at most once** for the life of the writer:

1. Keep a map `shard_idx → open handles` (or refuse reopening a previously closed shard with hard ERROR).
2. Prefer: on first visit, open with `"x"` (exclusive create) or `"a"` after ensuring empty; **never** `"w"` on an existing non-empty path mid-run.
3. Manifest descriptors: one entry per shard path; update `rows` only in `_close_current` / final close — do **not** append a second descriptor for the same path.
4. Optional safety: if `shard_idx < self._idx` (backwards jump), either ERROR with a clear message (recommended for fail-loud) or reopen append-only without truncate — pick one and test it. Prefer **ERROR** on non-monotonic shard progression so silent clobber is impossible.

**Verify**: read the method; `python -c "import ast; ast.parse(open('tools/wiki_extract.py').read())"`.

### Step 2: Unit tests

In `tests/test_wiki_extract.py`:

1. Write articles with ids that land in shard 0, then 1, then **back to shard 0** — expect ERROR (or append without data loss if that design is chosen; assert file content preserves first-write rows).
2. Monotonic progression across two shards: both files non-empty; manifest has **two** article descriptors with correct row counts; no duplicate paths.
3. Existing extract tests still pass.

**Verify**: `pytest tests/test_wiki_extract.py -q` → all green.

### Step 3: HTML extractor parity

If `wiki_extract_html.py` has the same `_rotate` / `open("w")` pattern, apply the same fix + a minimal test or shared helper.

**Verify**: `rg -n 'open\("w"' tools/wiki_extract*.py` — no shard rotate uses truncate-on-revisit.

## Test plan
- New cases above in `test_wiki_extract.py`.
- `make test && make lint`.

## Done criteria
- [ ] Shard reopen never truncates existing content
- [ ] Manifest does not accumulate duplicate path descriptors for one shard file
- [ ] Non-monotonic ids fail loud or preserve data (documented + tested)
- [ ] `make test` / `make lint` green
- [ ] Index DONE

## STOP conditions
- Extractor architecture changed so ids are guaranteed global-monotonic and shards never reopen — still eliminate `"w"` on revisit; do not rely on “shouldn’t happen”.
- Production manifests already corrupted — do not “fix” them in this plan; only stop future clobber.

## Maintenance notes
- Operators re-extracting enwiki must re-run `wiki_manifest_verify` after this lands.
- Reviewer: ensure `COPY`-escaping for categories still works after handle lifetime changes.
