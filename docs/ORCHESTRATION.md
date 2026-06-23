# TriDB Autonomous Build Orchestration

How TriDB work is driven autonomously: a fan-out of specialist agents, a local-model
delegation lane, and a standing Linus review loop. Captured here so the pattern is
repeatable across sessions.

## Three execution lanes

| Lane | Runs on | Good for | Verification |
| -- | -- | -- | -- |
| **Claude persona agents** | Cloud (via Workflow tool) | Design ADRs, harness logic, C-interface skeletons, judgment work | Linus review stage |
| **Local model** (`qwen3-coder`, dual GTX 1070) | This workstation, single GPU lane | Mechanical, well-scoped, *locally verifiable* codegen (corpus generator, reference models) | Run it here: `pytest`, `ruff` |
| **GX10** (ARM64 + CUDA) | Target hardware (remote) | The MSVBASE fork build + native C access method + live benchmark | `scripts/gx10build.sh`, on-target tests |

Routing rule: if a task's output can be *run and checked on this box*, send it to the
local model (cheap, private, no code round-trips through Claude's output). If it needs
judgment or cross-file reasoning, use a Claude agent. If it needs the live Postgres fork,
it is GX10-gated — produce a contract/skeleton instead and flag it.

## The kickoff workflow (`tridb-kickoff`)

A two-phase pipeline (script: `~/.claude/.../workflows/scripts/tridb-kickoff-*.js`):

1. **Author** — persona agents write one artifact each, in parallel:
   - `liotta` → `docs/decisions/0001-architecture-overview.md`
   - `postgres-dba` → `docs/graph_store_layout_v0.1.0.md` + ADR-0002 (DEV-1163)
   - `scribe` → `docs/sqlpgq_logical_plan_v0.1.0.md` (DEV-1167), `docs/join_order_heuristic_v0.1.0.md` (DEV-1170)
   - `compliant-implementer` → `baseline/` harness (DEV-1171), `src/graph_store/` C skeleton
2. **Review** — `linus-code-review` reads each artifact, applies critical/major fixes
   *in place* with Edit, and returns a structured verdict (critical/major/minor counts).
   The two local-model Python files are reviewed in this same pass.

Pipeline (not barrier): each artifact is reviewed the moment its author finishes, so the
graph-layout review runs while the baseline harness is still being written.

## The Linus review loop

The review stage above is one turn of the loop. Standing cadence for ongoing work:

```
/loop <interval> review the latest TriDB changes with the linus-code-review agent;
      apply critical fixes, summarize the rest
```

- Fires on new commits / working-tree changes; Linus reviews the diff against the golden
  rules in `CLAUDE.md` (TR-1 early termination, never-leave-Postgres, native-not-relational).
- Critical findings fixed in place + committed; major/minor surfaced for the operator.
- Stop with the loop's stop control when a milestone closes.

## Per-issue dispatch (going forward)

For a single gated issue once the GX10 build exists, prefer the GSD pipeline:
`/ship-issue DEV-1164` → plan → `compliant-implementer` → `test-engineer` → `linus-code-review`
→ atomic commit on `dustin/dev-1164` → PR. Foreman can chain multiple issues with worktree
isolation for parallel legs (e.g. DEV-1167 and DEV-1168 are independent).
