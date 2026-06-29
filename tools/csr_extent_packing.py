"""
csr_extent_packing — pure, host-side packing calculator for the CSR-lite graph layout spike.

This is the deterministic, engine-independent half of Plan 009 (sorted-by-dst contiguous
per-vertex adjacency extents). It computes, for a vertex of a given out-degree, how many
32KB adjacency pages the sorted-extent layout needs, where the unsorted delta tail lives,
and the worst-case shared-WAL full-image cost of a single in-order insert. It mirrors the
on-disk constants from src/graph_store/gph_page.h so the design note's arithmetic can be
regression-tested without the MSVBASE fork.

It deliberately does NOT touch the engine: it is arithmetic over the page geometry, used to
back the design note's sizing tables and to feed the degree-adaptive container threshold.

All sizes are bytes. References:
  - BLCKSZ 32768, GphEdgeSlot 32 bytes, GphPageSpecial chain pointer — gph_page.h
  - PostgreSQL PageHeaderData (SizeOfPageHeaderData) = 24 bytes on PG 13.4
"""

from __future__ import annotations

from dataclasses import dataclass

# --- On-disk geometry (mirrors src/graph_store/gph_page.h; keep in sync) -----------------

BLCKSZ = 32768  # --with-blocksize=32; StaticAssertDecl in gph_page.h
EDGE_SLOT_SIZE = 32  # sizeof(GphEdgeSlot), static-asserted in gph_page.h
PAGE_HEADER_SIZE = 24  # SizeOfPageHeaderData on PG 13.4

# GphPageSpecial = {uint16, uint16, BlockNumber(uint32), uint64} = 16 bytes, then MAXALIGN(8).
# GPH_SPECIAL_SIZE = MAXALIGN(sizeof(GphPageSpecial)) = 16 here (already 8-aligned).
SPECIAL_SIZE = 16


def usable_bytes_per_page() -> int:
    """Bytes available for packed EdgeSlots on one adjacency page (after header + special)."""
    return BLCKSZ - PAGE_HEADER_SIZE - SPECIAL_SIZE


def slots_per_page() -> int:
    """Whole EdgeSlots that fit on one adjacency page."""
    return usable_bytes_per_page() // EDGE_SLOT_SIZE


@dataclass(frozen=True)
class ExtentLayout:
    """Result of packing a vertex's adjacency list under the CSR-lite layout."""

    degree: int  # total out-edges (sorted run + delta tail)
    sorted_run: int  # edges kept in the sorted contiguous run
    delta_tail: int  # edges in the unsorted append delta (merged at maintenance)
    pages: int  # 32KB adjacency pages the whole list occupies
    slots_per_page: int  # capacity per page (for cross-checking)
    is_hub: bool  # True if degree >= hub_threshold (degree-adaptive container)
    wal_full_image_bytes: int  # worst-case shared-WAL cost of one in-order insert


def pack_extent(
    degree: int,
    *,
    delta_tail_cap: int | None = None,
    hub_threshold: int | None = None,
) -> ExtentLayout:
    """
    Compute the page layout for a vertex of out-degree `degree` under the CSR-lite design.

    delta_tail_cap: max edges held unsorted in the delta tail before a maintenance merge.
        Defaults to one page worth of slots (the design's "delta tail = trailing page" rule):
        small vertices never trigger a merge; only a vertex that fills a whole page of
        unsorted inserts pays the re-sort.
    hub_threshold: out-degree at/above which the vertex is a "hub" and uses the
        degree-adaptive (separately-grown) container. Defaults to one page of slots — i.e.
        a vertex whose sorted run no longer fits a single page is a hub.

    WAL accounting: a sorted in-order insert that lands inside an already-resident page
    dirties exactly that one page; under GenericXLog the worst case logged is one page
    FULL_IMAGE (the whole 32KB block), versus the current append path which also logs one
    page image but only shifts pd_lower. The *extra* amplification of sorted insert is the
    in-page memmove of the slots after the insertion point, not extra logged pages — both
    log one page image. The amplification shows up only when an in-order insert into a FULL
    sorted run must split/shift across the page boundary; see design note §4.
    """
    if degree < 0:
        raise ValueError("degree must be >= 0")

    spp = slots_per_page()
    if delta_tail_cap is None:
        delta_tail_cap = spp
    if hub_threshold is None:
        hub_threshold = spp
    if delta_tail_cap < 0:
        raise ValueError("delta_tail_cap must be >= 0")
    if hub_threshold < 1:
        raise ValueError("hub_threshold must be >= 1")

    # The delta tail holds at most delta_tail_cap edges; the rest is the sorted run.
    delta_tail = min(degree, delta_tail_cap)
    sorted_run = degree - delta_tail

    pages = (degree + spp - 1) // spp if degree > 0 else 0
    is_hub = degree >= hub_threshold

    # One sorted insert logs at most one full page image under GenericXLog.
    wal_full_image = BLCKSZ if degree > 0 else 0

    return ExtentLayout(
        degree=degree,
        sorted_run=sorted_run,
        delta_tail=delta_tail,
        pages=pages,
        slots_per_page=spp,
        is_hub=is_hub,
        wal_full_image_bytes=wal_full_image,
    )


def sorted_insert_shift_slots(position: int, run_len: int) -> int:
    """
    Slots that an in-place sorted insert must memmove to keep the run ordered.

    Inserting a new dst at `position` (0-based) into a sorted run of `run_len` live slots
    shifts every slot from `position` to the end: run_len - position. This is the
    write-amplification quantum of the in-place strategy (bytes moved = slots * 32). The
    delta-tail strategy reduces the *expected* shift to ~0 per insert (append to tail),
    paying it in bulk only at the periodic merge.
    """
    if not (0 <= position <= run_len):
        raise ValueError("position must be in [0, run_len]")
    return run_len - position


def merge_cost_slots(sorted_run: int, delta_tail: int) -> int:
    """
    Slots touched by a maintenance merge of the delta tail into the sorted run.

    A merge re-sorts `delta_tail` entries and merges them into `sorted_run`, rewriting the
    union: sorted_run + delta_tail slots written. This is the amortized price the delta-tail
    strategy pays, incurred once per delta_tail_cap inserts rather than once per insert.
    """
    if sorted_run < 0 or delta_tail < 0:
        raise ValueError("counts must be >= 0")
    return sorted_run + delta_tail


def amortized_shift_per_insert(delta_tail_cap: int, sorted_run: int) -> float:
    """
    Amortized slots moved per insert under the delta-tail strategy.

    Across one full delta cycle (delta_tail_cap inserts), the only bulk movement is one
    merge of (sorted_run + delta_tail_cap) slots. Per insert that is
    (sorted_run + delta_tail_cap) / delta_tail_cap. Contrast in-place sorted insert, whose
    expected shift is ~run_len/2 *every* insert. This ratio is the design's core
    write-amplification argument (design note §4).
    """
    if delta_tail_cap < 1:
        raise ValueError("delta_tail_cap must be >= 1")
    if sorted_run < 0:
        raise ValueError("sorted_run must be >= 0")
    return (sorted_run + delta_tail_cap) / delta_tail_cap


if __name__ == "__main__":
    spp = slots_per_page()
    print(f"BLCKSZ={BLCKSZ} usable={usable_bytes_per_page()} slots_per_page={spp}")
    for d in (1, 8, 100, spp, spp + 1, 10 * spp):
        lay = pack_extent(d)
        print(
            f"degree={d:>6} pages={lay.pages:>4} sorted_run={lay.sorted_run:>6} "
            f"delta={lay.delta_tail:>4} hub={lay.is_hub}"
        )
