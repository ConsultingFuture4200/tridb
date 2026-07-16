"""Guardrails for public Wiki-scale claims (advisor plan 080).

Static checks over both public copies of the landing page — `site/index.html`
and the `LANDING_HTML` string embedded in `tools/wiki_reader.py` — asserting:

1. Prohibited attributions are absent (the 6.9M reader run presented as
   native TriDB/Postgres execution; "native shortest-path" when NumPy CSR
   computed it; 0.71% without its host-reference qualifier).
2. Required provenance phrases are present (offline reader + its index stack;
   native engine evidence named only at its measured scales).
3. Key claim strings are identical across the two copies so one cannot
   drift silently.

Evidence anchors:
- docs/benchmark_wiki_scale_h2h_v0.2.0.md — native graph-inclusive @ 200K,
  native vector-only @ 1M; a full 6.9M native run was NOT performed.
- docs/benchmark_tjs_open_ref_v0.1.0.md — 0.71% examined is from a
  1,490-paragraph HotpotQA host reference corpus (Plan 007), not the wiki.
- docs/offline_wiki_reader_v0.1.0.md — the reader stack is SQLite metadata +
  NumPy CSR adjacency + cuVS CAGRA vectors, host-side, not Postgres.

No corpus, no server; runs under `make test`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _normalize(html_text: str) -> str:
    """Strip tags and collapse whitespace so claims match regardless of markup."""
    no_tags = re.sub(r"<[^>]+>", " ", html_text)
    return re.sub(r"\s+", " ", no_tags).strip()


def _load_site() -> str:
    return (ROOT / "site" / "index.html").read_text(encoding="utf-8")


def _load_embedded() -> str:
    src = (ROOT / "tools" / "wiki_reader.py").read_text(encoding="utf-8")
    m = re.search(r'^LANDING_HTML = """(.*?)^"""', src, re.S | re.M)
    assert m, "LANDING_HTML block not found in tools/wiki_reader.py"
    return m.group(1)


SURFACES = {
    "site/index.html": _normalize(_load_site()),
    "tools/wiki_reader.py::LANDING_HTML": _normalize(_load_embedded()),
}

# --- 1. Prohibited attributions (checked case-insensitively, tags stripped) ---

PROHIBITED = [
    # the full-corpus reader presented as the Postgres-native engine
    "running on tridb",
    "demonstrated on all of wikipedia",
    "wikipedia is the demonstration",
    # 6.9M presented as a native-fusion proof point
    "proves the fusion at",
    # reader-side structures presented as native engine execution
    "a native graph to traverse",
    "native shortest-path",
]

# --- 2. Required provenance phrases (must appear in every public copy) ------

REQUIRED = [
    # corpus size is kept, honestly scoped
    "6.9M",
    # the full-corpus experience is the offline reader over reader-side indexes
    "served by an offline reader (SQLite metadata, NumPy CSR link graph, "
    "cuVS CAGRA vectors)",
    # native engine evidence only at its measured scales
    "measured at 200K articles graph-inclusive and 1M vector-only",
    "largest graph-inclusive measurement is 200K articles (vector leg: 1M)",
    # the 0.71% work ratio carries its host-reference provenance
    "1,490-paragraph HotpotQA host reference corpus",
]

# --- 3. Key claims that must not drift between the two copies ---------------

KEY_CLAIMS = REQUIRED[1:]  # every provenance phrase except the bare scale


@pytest.mark.parametrize("name", sorted(SURFACES))
@pytest.mark.parametrize("phrase", PROHIBITED)
def test_prohibited_attribution_absent(name: str, phrase: str) -> None:
    assert phrase not in SURFACES[name].lower(), (
        f"{name}: prohibited attribution present: {phrase!r}"
    )


@pytest.mark.parametrize("name", sorted(SURFACES))
@pytest.mark.parametrize("phrase", REQUIRED)
def test_required_provenance_present(name: str, phrase: str) -> None:
    assert phrase in SURFACES[name], (
        f"{name}: required provenance phrase missing: {phrase!r}"
    )


@pytest.mark.parametrize("name", sorted(SURFACES))
def test_work_ratio_carries_host_reference_qualifier(name: str) -> None:
    """Every occurrence of 0.71 must sit near its 1,490-paragraph qualifier."""
    text = SURFACES[name]
    occurrences = [m.start() for m in re.finditer(re.escape("0.71"), text)]
    assert occurrences, f"{name}: expected at least one qualified 0.71 mention"
    for pos in occurrences:
        window = text[max(0, pos - 300) : pos + 300]
        assert "1,490-paragraph" in window, (
            f"{name}: 0.71 at offset {pos} lacks its 1,490-paragraph "
            f"host-reference qualifier within 300 chars"
        )


@pytest.mark.parametrize("claim", KEY_CLAIMS)
def test_key_claims_identical_across_copies(claim: str) -> None:
    presence = {name: claim in text for name, text in SURFACES.items()}
    assert all(presence.values()), (
        f"key claim drift between copies: {claim!r} -> {presence}"
    )
