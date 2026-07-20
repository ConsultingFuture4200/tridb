"""Release version-coherence lockstep gate (advisor plan 101).

One version string must agree everywhere a release stamps it: the two extension
.control files (the single source of truth), the release-image OCI label
(scripts/pg17/Dockerfile.release), the release notes (docs/releases/v<V>.md), the
maintainer checklist tag command (docs/releases/CHECKLIST.md), and the README's
release pointer. The publish workflow (.github/workflows/release.yml) additionally
refuses any tag != v<V> at run time; this test keeps the *tree* coherent so that
check can only ever fail on a mistyped tag, never on drifted files.

It also pins the byte-consistency contract: the release workflow's image build and
runtime-smoke `run:` lines are IDENTICAL to the CI `stock-pg` job's release steps,
so what publishes on a tag is exactly what CI (and `make stock-release-smoke`)
already proved.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

CONTROLS = [
    ROOT / "src" / "graph_store" / "graph_store_am.control",
    ROOT / "src" / "tjs_pg" / "tjs_pg.control",
]
DOCKERFILE_RELEASE = ROOT / "scripts" / "pg17" / "Dockerfile.release"
RELEASE_YML = ROOT / ".github" / "workflows" / "release.yml"
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"
CHECKLIST = ROOT / "docs" / "releases" / "CHECKLIST.md"
README = ROOT / "README.md"

# The exact steps a release image must go through, byte-identical in CI and the
# publish workflow (the ${{ matrix.pg }} interpolation included).
BUILD_RUN = (
    "docker build --build-arg PG_MAJOR=${{ matrix.pg }} "
    "-f scripts/pg17/Dockerfile.release -t tridb/postgres-trimodal:pg${{ matrix.pg }} ."
)
SMOKE_RUN = (
    "bash scripts/pg17_release_smoke.sh tridb/postgres-trimodal:pg${{ matrix.pg }}"
)


def control_version(path: Path) -> str:
    m = re.search(r"^default_version = '([^']+)'$", path.read_text(), re.M)
    assert m, f"no default_version in {path}"
    return m.group(1)


def version() -> str:
    versions = {p.name: control_version(p) for p in CONTROLS}
    assert len(set(versions.values())) == 1, f".control versions disagree: {versions}"
    return next(iter(versions.values()))


def test_extension_upgrade_scripts_exist_for_version():
    v = version()
    for ext_dir, ext in (("graph_store", "graph_store_am"), ("tjs_pg", "tjs_pg")):
        base = ROOT / "src" / ext_dir / f"{ext}--{v}.sql"
        upgrade = ROOT / "src" / ext_dir / f"{ext}--0.1.0--{v}.sql"
        assert base.is_file(), f"missing base script {base}"
        assert upgrade.is_file(), f"missing upgrade script {upgrade}"


def test_release_image_label_matches_control_version():
    v = version()
    text = DOCKERFILE_RELEASE.read_text()
    assert f'org.opencontainers.image.version="{v}"' in text, (
        f"Dockerfile.release OCI version label != .control version {v}"
    )


def test_release_notes_exist_and_carry_the_version():
    v = version()
    notes = ROOT / "docs" / "releases" / f"v{v}.md"
    assert notes.is_file(), f"release notes {notes} missing for .control version {v}"
    text = notes.read_text()
    assert f"v{v}" in text.splitlines()[0], f"notes title does not name v{v}"
    for ext in ("graph_store_am", "tjs_pg"):
        assert f"{ext} {v}" in text, f"notes do not state {ext} {v}"


def test_checklist_tags_the_control_version():
    v = version()
    text = CHECKLIST.read_text()
    assert f"git tag -a v{v}" in text, f"CHECKLIST tag command is not v{v}"
    assert f"v{v}.md" in text, "CHECKLIST does not point at the versioned notes"


def test_readme_points_at_the_versioned_release_notes():
    v = version()
    text = README.read_text()
    assert f"docs/releases/v{v}.md" in text, f"README release pointer is not v{v}"


def test_release_workflow_parses_and_is_byte_consistent_with_ci():
    wf = yaml.safe_load(RELEASE_YML.read_text())
    # `on:` parses as YAML boolean True.
    triggers = wf.get("on", wf.get(True))
    assert triggers["push"]["tags"] == ["v*"], "release must trigger on v* tags"
    assert "workflow_dispatch" in triggers, "dry-run mode (workflow_dispatch) removed"

    release_text = RELEASE_YML.read_text()
    ci_text = CI_YML.read_text()
    for step in (BUILD_RUN, SMOKE_RUN):
        assert step in release_text, f"release.yml drifted from the pinned step: {step}"
        assert step in ci_text, f"ci.yml drifted from the pinned step: {step}"

    publish = wf["jobs"]["build-smoke-publish"]
    assert publish["permissions"]["packages"] == "write", (
        "GHCR push needs packages: write"
    )
    gh_release = wf["jobs"]["github-release"]
    assert gh_release["permissions"]["contents"] == "write", (
        "gh release create needs contents: write"
    )
    assert gh_release["needs"] == "build-smoke-publish", (
        "the Release must publish only after both images built, smoked, and pushed"
    )
    assert "docs/releases/$GITHUB_REF_NAME.md" in release_text, (
        "the Release body must come from the versioned notes file"
    )
