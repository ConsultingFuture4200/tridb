"""Contamination regression tests for the isolated GB10 GPU env (advisor plan 086).

The GPU setup path must never touch the core .venv / requirements.lock: it targets
a dedicated ${GPU_VENV:-.venv-gpu}, installs ONLY from the GB10-generated lock
(requirements-gpu-gb10.lock), and is a clean SKIP on any non-GB10 host. These
tests are static (script/manifest text) plus one real off-target SKIP execution —
no GPU claims are made here; the GPU verification gate runs on the Spark only.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "spark_gpu_setup.sh"
GPU_IN = ROOT / "requirements-gpu-gb10.in"
GPU_LOCK = ROOT / "requirements-gpu-gb10.lock"
MAKEFILE = ROOT / "Makefile"
GITIGNORE = ROOT / ".gitignore"

# module import name -> PyPI distribution name (as it must appear in the .in)
GPU_TOOL_IMPORTS = {
    "torch": "torch",
    "cuvs": "cuvs-cu13",
    "pylibraft": "pylibraft-cu13",
    "sentence_transformers": "sentence-transformers",
    "numpy": "numpy",
    "pandas": "pandas",
    "scipy": "scipy",
    "fastembed": "fastembed",
    "hnswlib": "hnswlib",
}

# The GPU-workload entry points whose direct imports the .in must cover.
GPU_TOOLS = [
    "tools/wiki_linkpredict.py",
    "tools/wiki_linkpredict_fused.py",
    "tools/wiki_embed_hybrid.py",
    "scripts/gpu_build_index.py",
]

_IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)


def _script_lines():
    return SCRIPT.read_text().splitlines()


def _code_lines():
    """Script lines with comments/blank lines stripped."""
    out = []
    for ln in _script_lines():
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(ln.split(" #")[0])
    return out


def test_setup_targets_isolated_gpu_venv_never_core_venv():
    code = "\n".join(_code_lines())
    # The venv variable must default to the isolated GPU venv with an override.
    assert 'VENV="${GPU_VENV:-$ROOT/.venv-gpu}"' in code, (
        "spark_gpu_setup.sh must target ${GPU_VENV:-$ROOT/.venv-gpu}"
    )
    # No code path may reference the core .venv (mask the .venv-gpu literal first).
    masked = code.replace(".venv-gpu", "<GPUVENV>")
    assert ".venv" not in masked, (
        "spark_gpu_setup.sh references the core .venv — contamination regression"
    )


def test_all_installs_consume_the_gpu_lock_not_package_names():
    code = "\n".join(_code_lines())
    # $LOCK is anchored to the GB10 lock file, and every install consumes it.
    assert 'LOCK="$ROOT/requirements-gpu-gb10.lock"' in code
    install_lines = [ln for ln in code.splitlines() if re.search(r"pip install", ln)]
    assert install_lines, "expected at least one pip install line (the lock install)"
    for ln in install_lines:
        assert re.search(r'-r\s+("\$LOCK"|requirements-gpu-gb10\.lock)', ln), (
            f"floating/named-package install (must be -r the GB10 lock): {ln!r}"
        )
        assert "--require-hashes" in ln, f"lock install must enforce hashes: {ln!r}"


def test_offtarget_guard_skips_before_any_venv_work():
    text = SCRIPT.read_text()
    assert "SKIP: GB10/CUDA unavailable" in text
    # The GB10 lock is aarch64-only; the guard must also reject non-aarch64 hosts.
    assert "aarch64" in text


def test_direct_gpu_requirements_are_exact_pins():
    assert GPU_IN.exists(), "requirements-gpu-gb10.in missing"
    reqs = {}
    for ln in GPU_IN.read_text().splitlines():
        ln = ln.split("#")[0].strip()
        if not ln:
            continue
        m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9.+!]+)", ln)
        assert m, f"not an exact == pin: {ln!r}"
        reqs[m.group(1).lower()] = m.group(2)
    assert reqs, "requirements-gpu-gb10.in has no requirements"


def test_every_direct_gpu_tool_import_is_pinned_in_the_input():
    pinned = {
        re.split(r"==", ln.split("#")[0].strip())[0].lower()
        for ln in GPU_IN.read_text().splitlines()
        if "==" in ln.split("#")[0]
    }
    for tool in GPU_TOOLS:
        src = (ROOT / tool).read_text()
        for mod in _IMPORT_RE.findall(src):
            dist = GPU_TOOL_IMPORTS.get(mod)
            if dist is None:
                continue  # stdlib / repo-local / non-GPU module
            assert dist.lower() in pinned, (
                f"{tool} imports {mod} but {dist} is not pinned in requirements-gpu-gb10.in"
            )


def test_gpu_lock_is_a_full_closure_superset_of_the_input():
    assert GPU_LOCK.exists(), "requirements-gpu-gb10.lock missing (generate on GB10)"
    lock_text = GPU_LOCK.read_text()
    lock_names = {
        m.group(1).lower().replace("_", "-")
        for m in re.finditer(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==", lock_text, re.M)
    }
    assert len(lock_names) > 20, "lock does not look like a full transitive closure"
    for ln in GPU_IN.read_text().splitlines():
        ln = ln.split("#")[0].strip()
        if "==" not in ln:
            continue
        name = ln.split("==")[0].lower().replace("_", "-")
        assert name in lock_names, f"direct pin {name} missing from the lock"


def test_gitignore_ignores_gpu_venv():
    assert ".venv-gpu/" in GITIGNORE.read_text().splitlines()


def test_core_lock_target_unchanged_and_gpu_targets_isolated():
    mk = MAKEFILE.read_text()
    # Core lock recipe still freezes the core .venv into requirements.lock.
    assert "VIRTUAL_ENV=.venv uv pip freeze" in mk
    lock_recipe = mk.split("\nlock:\n", 1)[1].split("\n\n", 1)[0]
    assert "requirements.lock" in lock_recipe
    assert "gpu" not in lock_recipe.lower(), (
        "core lock target must not know about GPU env"
    )
    # GPU targets exist and delegate to the setup script only.
    for tgt in ("gpu-setup:", "gpu-verify:", "gpu-lock:"):
        assert f"\n{tgt}" in mk, f"missing Make target {tgt}"
        recipe = mk.split(f"\n{tgt}", 1)[1].split("\n\n", 1)[0]
        assert "spark_gpu_setup.sh" in recipe
        assert "requirements.lock" not in recipe.replace(
            "requirements-gpu-gb10.lock", ""
        )


def test_offtarget_run_prints_skip_creates_nothing_never_claims_success(tmp_path):
    """Execute the script with nvidia-smi absent: explicit SKIP, exit 0, no venv,
    no success marker, core lock byte-identical."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Minimal PATH without nvidia-smi (script needs dirname/uname for ROOT/arch).
    for exe in ("dirname", "uname"):
        src = shutil.which(exe)
        assert src, f"{exe} not found on host"
        os.symlink(src, bin_dir / exe)
    gpu_venv = tmp_path / "gpu-venv"
    core_lock_before = (ROOT / "requirements.lock").read_bytes()
    proc = subprocess.run(
        [shutil.which("bash"), str(SCRIPT)],
        env={"PATH": str(bin_dir), "GPU_VENV": str(gpu_venv)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        f"off-target run must exit 0, got {proc.returncode}: {out}"
    )
    assert "SKIP: GB10/CUDA unavailable" in out
    assert "ALL GPU PATHS VERIFIED" not in out, (
        "off-target run printed the success marker"
    )
    assert not gpu_venv.exists(), "off-target run created a venv"
    assert (ROOT / "requirements.lock").read_bytes() == core_lock_before
