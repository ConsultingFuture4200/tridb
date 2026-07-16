"""src/tjs_pg must never call graph_store.gph_traverse_bfs (plan 077 / ADR-0020 decision 4).

gph_traverse_bfs is kept, deliberately, as a documented MATERIALIZING TEST/ORACLE helper:
the whole bounded-depth reach is computed at Open, before the first row is ever served --
exactly the TR-1 violation plan 077 fixed for the stock tjs_open operator. The operator's
graph leg must go through the bounded pull iterator (graph_store.gph_traverse_bounded)
exclusively.

test/tjs_pg_tr1_test.sql exercises this behaviorally, but only against its own fixture --
a reintroduced call to the whole-BFS helper elsewhere in the operator wouldn't necessarily
be caught by that suite's specific queries. This guard is unconditional and structural: it
greps every src/tjs_pg/*.c file (mirroring the manual verification command
`rg 'gph_traverse_bfs' src/tjs_pg`) rather than relying on any one behavioral fixture.
"""

from pathlib import Path

SRC_TJS_PG = Path(__file__).resolve().parents[1] / "src" / "tjs_pg"

BANNED = "gph_traverse_bfs"


def test_no_gph_traverse_bfs_reference_in_tjs_pg_sources():
    c_files = sorted(SRC_TJS_PG.glob("*.c"))
    assert c_files, f"expected at least one .c file under {SRC_TJS_PG}"

    offenders = {}
    for path in c_files:
        text = path.read_text()
        if BANNED in text:
            lines = [
                i + 1 for i, line in enumerate(text.splitlines()) if BANNED in line
            ]
            offenders[path.name] = lines

    assert not offenders, (
        f"{BANNED} must not appear anywhere in src/tjs_pg/*.c (banned from the operator "
        f"path, ADR-0020 decision 4 -- it is a materializing test/oracle helper only): "
        f"{offenders}"
    )
