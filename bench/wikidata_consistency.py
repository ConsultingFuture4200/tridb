"""Harness A — Wikidata edit-firehose CROSS-MODAL CONSISTENCY (plan 060, ADR-0018).

The Wikidata analogue of bench/wiki_consistency.py. Where the wiki harness bumped a synthetic
version across the three legs, this REPLAYS a window of the Wikidata edit stream — the genuine
*mutation* workload Wikipedia lacks (millions of edits/day). Each Wikidata edit is a cross-modal
update to one entity:

  * a label change      -> the entity's embedding  (VECTOR leg)
  * add/remove a typed statement -> a graph out-edge (GRAPH leg)
  * a claim change      -> a relational column      (REL leg)

An entity is CONSISTENT iff the three legs agree on the edit revision `rev`, TORN iff they
disagree. The claim (ADR-0017): TriDB applies all three legs in ONE transaction under ONE WAL, so
a concurrent reader is never torn; a Milvus+Neo4j+Postgres stack commits the three legs
independently, so a reader interleaved with an edit catches a torn cross-modal state. **Headline:
torn cross-modal reads, TriDB vs multi-store, replaying M edits.**

TWO LAYERS, because the live stores are GX10/Spark-only:

  1. HOST SIMULATION (`simulate`, runs anywhere, unit-tested). A DETERMINISTIC model of the two
     architectures over the edit stream: a reader that samples the in-flight entity after EVERY
     store commit (maximal-exposure reader). The one-WAL architecture commits an edit atomically
     (one observable step) so the reader is never torn; the multi-store commits leg-by-leg so the
     reader is torn on every pre-final leg. This proves the STRUCTURAL difference with no DB — it
     is a model of the architecture, not a timing measurement.

  2. LIVE REPLAY (`run_live`, `--live`, GX10/Spark-gated). Replays the SAME edit stream against the
     real engine (one txn/edit) and the real Milvus+Neo4j+Postgres stack (independent commits),
     reusing bench/wiki_consistency's engine_*/MultiStore/torn primitives verbatim, and tallies the
     REAL timing-dependent torn-read rate. This is where the measured number comes from.

HONESTY: the multi-store tear is INHERENT to having no cross-system transaction, not a
Milvus/Neo4j/Postgres bug (each store is internally consistent; the simulation reads reflect only
CROSS-store disagreement). It is mitigable app-side (2PC/saga/outbox) at real cost; TriDB gives
cross-modal ACID for free. The simulation's multi-store rate is the STRUCTURAL upper bound (reader
after every commit); the live rate is timing-dependent and lower. Nothing is fabricated.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

# Reuse the torn predicate verbatim (plan 060: "Reuse torn()"). Importing wiki_consistency also
# makes its live engine_*/MultiStore primitives available for the --live path below.
from bench.wiki_consistency import torn

# The three modal legs of an entity, in multi-store commit order (vector, graph, relational).
LEGS = ("vector", "graph", "relational")
BASELINE_REV = 0


# ======================================================================================
# Edit model — a window of the Wikidata edit firehose
# ======================================================================================
@dataclass(frozen=True)
class Edit:
    """One cross-modal edit: entity `entity` advances all three legs to revision `rev`.

    Matches the wiki_consistency model and the plan's described edit (label->embedding,
    statement->edge, claim->relational row all move together). Consistency == the three legs
    agree on `rev`; the demo is whether that advance is committed atomically (one WAL) or
    leg-by-leg (multi-store).
    """

    entity: int
    rev: int


def parse_edit(obj: dict) -> Edit | None:
    """Parse one recorded-edit JSON object into an Edit, or None if it is unusable.

    Recorded-sample schema (a trimmed EventStreams `recentchange`):
        {"entity": <Q-int>, "rev": <int>, "label": <str?>, "statement": <[p,dst]?>,
         "claim": <obj?>}
    label/statement/claim are the modal payloads the edit carried (informational — they are
    what make it a cross-modal edit); the consistency model advances all three legs to `rev`.
    """
    ent = obj.get("entity")
    rev = obj.get("rev")
    if not isinstance(ent, int) or not isinstance(rev, int):
        return None
    return Edit(entity=ent, rev=rev)


def load_edits(path: Path) -> list[Edit]:
    """Load a recorded edit window (JSONL, one edit object per line)."""
    edits: list[Edit] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        e = parse_edit(json.loads(line))
        if e is not None:
            edits.append(e)
    return edits


def synthetic_edits(n_edits: int, n_entities: int, seed: int) -> list[Edit]:
    """A deterministic synthetic edit window: `n_edits` full cross-modal bumps over `n_entities`.

    Each entity's revision increases by one every time it is edited, so replays are monotone per
    entity (as the real firehose is). Deterministic given `seed` — the dry-run needs no dump.
    """
    rng = random.Random(seed)
    rev_of: dict[int, int] = {}
    edits: list[Edit] = []
    for _ in range(n_edits):
        ent = rng.randrange(n_entities)
        rev_of[ent] = rev_of.get(ent, BASELINE_REV) + 1
        edits.append(Edit(entity=ent, rev=rev_of[ent]))
    return edits


# ======================================================================================
# Host simulation — the deterministic architecture demonstrator (runs anywhere)
# ======================================================================================
def _commit_observations(
    state: dict[int, dict[str, int]], edit: Edit, atomic: bool
) -> Iterator[tuple[int, int, int]]:
    """Apply one edit to `state`, yielding the in-flight entity's 3-leg state after each commit.

    atomic=True (one WAL): all three legs move together -> a SINGLE observation, consistent.
    atomic=False (multi-store): each leg is an independent commit -> one observation per leg, the
    pre-final ones exposing a torn cross-modal state to a concurrent reader.
    """
    legs = state[edit.entity]
    if atomic:
        for leg in LEGS:
            legs[leg] = edit.rev
        yield (legs["vector"], legs["graph"], legs["relational"])
        return
    for leg in LEGS:
        legs[leg] = edit.rev
        yield (legs["vector"], legs["graph"], legs["relational"])


def simulate(edits: Iterable[Edit], atomic: bool) -> dict:
    """Replay `edits` under one architecture; a reader samples after every commit. Tally torn.

    Returns {observations, torn, torn_rate, examples}. `atomic=True` models TriDB's one-WAL edit
    (0 torn); `atomic=False` models the independent-commit multi-store (torn on every pre-final
    leg). The reader watches the in-flight entity — the only entity a single-threaded replay can
    tear — which is the maximal-exposure (hot-entity) reader.
    """
    edits = list(edits)
    entities = {e.entity for e in edits}
    state: dict[int, dict[str, int]] = {
        ent: {leg: BASELINE_REV for leg in LEGS} for ent in entities
    }
    observations = 0
    torn_count = 0
    examples: list[dict] = []
    for edit in edits:
        for legs in _commit_observations(state, edit, atomic):
            observations += 1
            if torn(legs):
                torn_count += 1
                if len(examples) < 4:
                    examples.append(
                        {
                            "entity": edit.entity,
                            "rev": edit.rev,
                            "vector": legs[0],
                            "graph": legs[1],
                            "relational": legs[2],
                        }
                    )
    return {
        "architecture": "one_wal_atomic"
        if atomic
        else "multistore_independent_commits",
        "edits": len(edits),
        "observations": observations,
        "torn": torn_count,
        "torn_rate": (torn_count / observations) if observations else 0.0,
        "examples": examples,
    }


def simulate_headtohead(edits: Iterable[Edit]) -> dict:
    """Run the host simulation for both architectures and package the head-to-head result."""
    edits = list(edits)
    tridb = simulate(edits, atomic=True)
    multistore = simulate(edits, atomic=False)
    return {
        "layer": "host_simulation",
        "note": (
            "deterministic model of the two architectures; the reader samples the in-flight "
            "entity after every store commit (maximal exposure). TriDB torn=0 is structural "
            "(atomic edit); the multi-store rate is the STRUCTURAL UPPER BOUND, not a timing "
            "measurement — the live replay (--live, GX10) measures the real, lower rate."
        ),
        "tridb": tridb,
        "multistore": multistore,
    }


# ======================================================================================
# Live replay — real engine vs real multi-store (GX10/Spark-gated)
# ======================================================================================
def run_live(edits: list[Edit], reads: int, gap_ms: float) -> dict:
    """Replay the edit stream against the REAL stores, tallying the real torn-read rate.

    GX10/Spark-ONLY: needs the loaded engine + the isolated tridb-wiki Milvus/Neo4j/pg baseline
    (bench/wiki_consistency's live layout). Reuses engine_setup/engine_write/engine_read and the
    live MultiStore verbatim. A concurrent reader hammers the hot entities while a writer replays
    the edits — TriDB one txn/edit, multi-store three independent commits/edit. Imported lazily so
    the host layer never requires a running engine.
    """
    import threading

    from bench.wiki_consistency import (
        MultiStore,
        engine_connect,
        engine_read,
        engine_setup,
        engine_write,
    )

    # Map the (sparse) edit entities onto the dense 0..M-1 the engine model uses.
    ents = sorted({e.entity for e in edits})
    idx = {q: i for i, q in enumerate(ents)}
    m = len(ents)
    gap = gap_ms / 1000.0

    # ---- TriDB: writer replays edits, each a one-txn multi-modal write; reader tallies torn ----
    engine_setup(m)
    rev_state = {i: 0 for i in range(m)}
    stop = threading.Event()

    def tri_writer():
        c = engine_connect(autocommit=False)
        for e in edits:
            if stop.is_set():
                break
            i = idx[e.entity]
            nv = (
                1 - rev_state[i]
            )  # the engine model flips between two dense target vertices
            with c.cursor() as cur:
                engine_write(cur, m, i, new_v=nv, old_v=rev_state[i])
            c.commit()
            rev_state[i] = nv
        c.close()

    tw = threading.Thread(target=tri_writer)
    tw.start()
    tri_torn = 0
    rc = engine_connect(autocommit=True)
    hot = idx[edits[0].entity]
    for _ in range(reads):
        with rc.cursor() as cur:
            legs = engine_read(cur, m, hot)
        if torn(legs):
            tri_torn += 1
    rc.close()
    stop.set()
    tw.join()

    # ---- Multi-store: independent-commit writer, sequential 3-store reader ----
    ms = MultiStore()
    ms.setup(m)
    stop2 = threading.Event()

    def ms_writer():
        w = MultiStore()
        rv = {i: 0 for i in range(m)}
        for e in edits:
            if stop2.is_set():
                break
            i = idx[e.entity]
            nv = 1 - rv[i]
            w.write(i, nv, gap=gap)
            rv[i] = nv
        w.close()

    mw = threading.Thread(target=ms_writer)
    mw.start()
    ms_torn = 0
    for _ in range(reads):
        if torn(ms.read(hot)):
            ms_torn += 1
    stop2.set()
    mw.join()
    ms.close()

    return {
        "layer": "live_replay",
        "edits": len(edits),
        "entities": m,
        "reads": reads,
        "writer_gap_ms": gap_ms,
        "tridb_torn_reads": tri_torn,
        "tridb_torn_rate": tri_torn / reads,
        "multistore_torn_reads": ms_torn,
        "multistore_torn_rate": ms_torn / reads,
    }


# ======================================================================================
# CLI
# ======================================================================================
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--replay",
        type=Path,
        default=None,
        help="recorded edit window (JSONL); else synthetic",
    )
    ap.add_argument(
        "--edits", type=int, default=300, help="synthetic edit count (no --replay)"
    )
    ap.add_argument("--m", type=int, default=50, help="synthetic distinct entity count")
    ap.add_argument("--seed", type=int, default=1354)
    ap.add_argument(
        "--live", action="store_true", help="run the GX10/Spark live replay too"
    )
    ap.add_argument("--reads", type=int, default=300, help="live reader sample count")
    ap.add_argument(
        "--gap-ms", type=float, default=5.0, help="live writer inter-store gap"
    )
    ap.add_argument(
        "--out", type=Path, default=Path("bench/results/wikidata_consistency.json")
    )
    args = ap.parse_args(argv)

    edits = (
        load_edits(args.replay)
        if args.replay
        else synthetic_edits(args.edits, args.m, args.seed)
    )
    result: dict = {
        "meta": {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": str(args.replay)
            if args.replay
            else f"synthetic(seed={args.seed})",
            "edits": len(edits),
        },
        "host_simulation": simulate_headtohead(edits),
    }
    hs = result["host_simulation"]
    print(
        f"[host sim] TriDB torn {hs['tridb']['torn']}/{hs['tridb']['observations']} "
        f"({100 * hs['tridb']['torn_rate']:.1f}%)  vs  multi-store "
        f"{hs['multistore']['torn']}/{hs['multistore']['observations']} "
        f"({100 * hs['multistore']['torn_rate']:.1f}%)"
    )
    if args.live:
        result["live_replay"] = run_live(edits, args.reads, args.gap_ms)
        lr = result["live_replay"]
        print(
            f"[live]     TriDB torn {lr['tridb_torn_reads']}/{lr['reads']} "
            f"({100 * lr['tridb_torn_rate']:.1f}%)  vs  multi-store "
            f"{lr['multistore_torn_reads']}/{lr['reads']} "
            f"({100 * lr['multistore_torn_rate']:.1f}%)"
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print(f"[wikidata_consistency] raw results -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
