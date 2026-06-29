"""
Validate the CSR-lite extent-packing calculator (Plan 009 host-side, engine-independent).

These tests pin the page geometry to the on-disk constants in src/graph_store/gph_page.h
and check the design note's sizing / write-amplification arithmetic. They run with `make
test` on any dev box — no MSVBASE fork required.
"""

import importlib.util
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MOD_PATH = ROOT / "tools" / "csr_extent_packing.py"

_spec = importlib.util.spec_from_file_location("csr_extent_packing", MOD_PATH)
csr = importlib.util.module_from_spec(_spec)
# Register before exec so dataclass type-resolution (with `from __future__ import
# annotations`) can find the module in sys.modules.
sys.modules["csr_extent_packing"] = csr
_spec.loader.exec_module(csr)


def test_geometry_matches_on_disk_constants():
    # gph_page.h: BLCKSZ 32768, GphEdgeSlot 32 bytes; PG13.4 header 24; special 16.
    assert csr.BLCKSZ == 32768
    assert csr.EDGE_SLOT_SIZE == 32
    assert csr.usable_bytes_per_page() == 32768 - 24 - 16
    # ADR-0002 Decision 2 quotes ~1021/1022 EdgeSlots per adjacency page.
    assert csr.slots_per_page() == 1022


def test_pack_empty_vertex():
    lay = csr.pack_extent(0)
    assert lay.pages == 0
    assert lay.sorted_run == 0
    assert lay.delta_tail == 0
    assert lay.is_hub is False
    assert lay.wal_full_image_bytes == 0


def test_pack_small_vertex_single_page_all_in_delta():
    # A low-degree vertex fits in one page and stays entirely in the unsorted delta tail
    # (default delta cap = one page), so it never pays a merge.
    lay = csr.pack_extent(8)
    assert lay.pages == 1
    assert lay.delta_tail == 8
    assert lay.sorted_run == 0
    assert lay.is_hub is False


def test_hub_threshold_at_one_page():
    spp = csr.slots_per_page()
    assert csr.pack_extent(spp - 1).is_hub is False
    assert csr.pack_extent(spp).is_hub is True
    assert csr.pack_extent(spp + 1).is_hub is True


def test_pages_grow_with_degree():
    spp = csr.slots_per_page()
    assert csr.pack_extent(spp).pages == 1
    assert csr.pack_extent(spp + 1).pages == 2
    assert csr.pack_extent(10 * spp).pages == 10
    # general ceiling relation
    for d in (1, 7, 999, 1023, 5000, 100000):
        assert csr.pack_extent(d).pages == math.ceil(d / spp)


def test_delta_tail_caps_and_sorted_run_takes_remainder():
    lay = csr.pack_extent(5000, delta_tail_cap=256)
    assert lay.delta_tail == 256
    assert lay.sorted_run == 5000 - 256


def test_delta_tail_cap_larger_than_degree_keeps_all_in_tail():
    lay = csr.pack_extent(10, delta_tail_cap=256)
    assert lay.delta_tail == 10
    assert lay.sorted_run == 0


def test_wal_full_image_is_one_page_for_any_nonempty():
    # The shared-WAL claim: a sorted in-order insert logs at most one 32KB full image,
    # same page-image count as today's append path (the amplification is in-page memmove,
    # not extra logged pages).
    for d in (1, 100, 10000):
        assert csr.pack_extent(d).wal_full_image_bytes == csr.BLCKSZ


def test_sorted_insert_shift_slots():
    # Inserting at the front of a 100-slot run shifts all 100; at the end shifts 0.
    assert csr.sorted_insert_shift_slots(0, 100) == 100
    assert csr.sorted_insert_shift_slots(100, 100) == 0
    assert csr.sorted_insert_shift_slots(40, 100) == 60
    with pytest.raises(ValueError):
        csr.sorted_insert_shift_slots(101, 100)


def test_merge_cost_slots():
    assert csr.merge_cost_slots(900, 100) == 1000
    assert csr.merge_cost_slots(0, 0) == 0


def test_amortized_shift_beats_in_place_for_large_runs():
    # The core write-amplification argument: with a 1022-slot delta cap, amortized
    # per-insert movement is far below the in-place expected shift (~run/2) for a large run.
    run = 100000
    cap = csr.slots_per_page()
    amort = csr.amortized_shift_per_insert(cap, run)
    in_place_expected = run / 2
    assert amort < in_place_expected
    # sanity: amortized = (run + cap) / cap
    assert amort == pytest.approx((run + cap) / cap)


def test_negative_degree_rejected():
    with pytest.raises(ValueError):
        csr.pack_extent(-1)
