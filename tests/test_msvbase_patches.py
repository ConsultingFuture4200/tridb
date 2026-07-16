"""SPTAG initialization predicate in the shared patch library (advisor plan 084).

Git leaves an EMPTY placeholder directory for a registered-but-uninitialized
submodule, so `[[ -d thirdparty/SPTAG ]]` was a false positive: verify_patches
took the placeholder for an initialized tree, then failed on the missing
spann-patch sentinel. The fix is a single side-effect-free predicate,
`sptag_initialized ROOT`, anchored on the load-bearing tracked file
`thirdparty/SPTAG/CMakeLists.txt`.

Four states are covered against the real verification seam:
  absent               -> spann check skipped (verify proceeds past it)
  empty placeholder    -> spann check skipped (the old false positive)
  initialized/unpatched -> spann check FAILS (verify_patches not weakened)
  initialized/patched  -> spann check passes (verify proceeds past it)
"""

import subprocess
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib" / "msvbase_patches.sh"

# The verify_patches check immediately AFTER the SPTAG block. Seeing this error
# proves the run got PAST the spann check (skipped or satisfied) in a minimal
# fixture that stops there.
NEXT_CHECK_ERR = "l2_distance_scalar"
SPANN_ERR = "spann.patch NOT applied"


def run_lib(snippet: str) -> subprocess.CompletedProcess:
    """Source the shared lib with stub log/die and run a snippet."""
    script = "\n".join(
        [
            "log() { :; }",
            'die() { printf "DIE: %s\\n" "$*" >&2; exit 1; }',
            f'source "{LIB}"',
            snippet,
        ]
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60
    )


def make_root(tmp_path: Path) -> Path:
    """Minimal MSVBASE tree satisfying the checks BEFORE the SPTAG block."""
    root = tmp_path / "msvbase"
    pg = root / "thirdparty" / "Postgres" / "src" / "include" / "access"
    pg.mkdir(parents=True)
    (pg / "amapi.h").write_text("bool amcanrelaxedorderbyop;\n")
    hnsw = root / "thirdparty" / "hnsw" / "hnswlib"
    hnsw.mkdir(parents=True)
    (hnsw / "hnswalg.h").write_text("class ResultIterator {};\n")
    return root


def init_sptag(root: Path, patched: bool) -> None:
    sptag = root / "thirdparty" / "SPTAG"
    sptag.mkdir(parents=True)
    (sptag / "CMakeLists.txt").write_text('set(CMAKE_CXX_FLAGS "-std=c++14")\n')
    if patched:
        hdr = sptag / "AnnService" / "inc" / "Core"
        hdr.mkdir(parents=True)
        (hdr / "MultiIndexScan.h").write_text("class MultiIndexScan {};\n")


# --- the predicate itself -------------------------------------------------


def test_predicate_absent_is_false(tmp_path):
    root = make_root(tmp_path)
    r = run_lib(f'sptag_initialized "{root}"')
    assert r.returncode != 0


def test_predicate_empty_placeholder_is_false(tmp_path):
    root = make_root(tmp_path)
    (root / "thirdparty" / "SPTAG").mkdir(parents=True)
    r = run_lib(f'sptag_initialized "{root}"')
    assert r.returncode != 0


def test_predicate_initialized_tree_is_true(tmp_path):
    root = make_root(tmp_path)
    init_sptag(root, patched=False)
    r = run_lib(f'sptag_initialized "{root}"')
    assert r.returncode == 0


def test_no_directory_only_sptag_decision_remains():
    """No SPTAG initialization decision may use directory existence alone."""
    text = LIB.read_text()
    assert "-d " not in "".join(
        line for line in text.splitlines() if "thirdparty/SPTAG" in line
    ), "found a bare directory-existence check on thirdparty/SPTAG"


# --- the verify_patches seam ----------------------------------------------


def verify(root: Path) -> subprocess.CompletedProcess:
    return run_lib(f'verify_patches "{root}"')


def test_verify_skips_when_sptag_absent(tmp_path):
    r = verify(make_root(tmp_path))
    assert r.returncode != 0
    assert SPANN_ERR not in r.stderr
    assert NEXT_CHECK_ERR in r.stderr  # got past the SPTAG block


def test_verify_skips_empty_placeholder(tmp_path):
    root = make_root(tmp_path)
    (root / "thirdparty" / "SPTAG").mkdir(parents=True)
    r = verify(root)
    assert r.returncode != 0
    assert SPANN_ERR not in r.stderr  # the old false positive
    assert NEXT_CHECK_ERR in r.stderr


def test_verify_fails_initialized_but_unpatched(tmp_path):
    root = make_root(tmp_path)
    init_sptag(root, patched=False)
    r = verify(root)
    assert r.returncode != 0
    assert SPANN_ERR in r.stderr


def test_verify_passes_spann_check_when_patched(tmp_path):
    root = make_root(tmp_path)
    init_sptag(root, patched=True)
    r = verify(root)
    assert r.returncode != 0
    assert SPANN_ERR not in r.stderr
    assert NEXT_CHECK_ERR in r.stderr  # spann satisfied; failed on the next check
