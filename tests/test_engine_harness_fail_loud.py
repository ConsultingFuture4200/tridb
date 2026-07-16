"""Engine container harnesses must preserve build failures (advisor plan 072).

Three engine harnesses run `make` inside a `bash -c` container payload. The host
shell's `set -euo pipefail` does NOT cross into that inner shell, and the inner
`set -e` does not inspect the non-final process of a pipeline. So the shape

    make ... 2>&1 | tail -N

reports `tail`'s zero exit status and silently turns a failed compile into a
green test run. These tests read each harness's inner payload and require that
every `make` invocation there is fail-loud: either `pipefail` is enabled in the
*inner* shell, or the build is captured to a log file and its status checked
explicitly (`|| { ...; exit 1; }`) — the pattern in scripts/graph_am_test.sh.
"""

import re
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

# Named explicitly so deleting or renaming a harness fails the test rather than
# making it vacuously pass.
HARNESSES = ["graph_test.sh", "pg17_graph_test.sh", "tjs_test.sh"]

# The `bash -c '...'` payload handed to the container. The payloads contain no
# single quotes, so the first closing quote ends the payload.
PAYLOAD_RE = re.compile(r"-c\s+'\n(.*?)'", re.DOTALL)

# pipefail enabled inside the payload: `set -o pipefail`, `set -eo pipefail`, ...
PIPEFAIL_RE = re.compile(r"\bset\s+-\w*o\s+pipefail\b")

# A make line piped into tail (the defect shape).
MAKE_TAIL_RE = re.compile(r"^.*\bmake\b[^|\n]*\|\s*tail\b.*$", re.MULTILINE)

# Any make invocation (build or install).
MAKE_LINE_RE = re.compile(r"^\s*(?:echo\b[^;\n]*;\s*)?(make\b.*)$", re.MULTILINE)

# The fail-loud log pattern: output redirected to a file, status checked with ||.
LOG_AND_CHECK_RE = re.compile(r">\s*\S+\s+2>&1\s*\|\|")


def inner_payload(name: str) -> str:
    path = SCRIPTS / name
    assert path.is_file(), f"harness {path} is missing"
    text = path.read_text()
    m = PAYLOAD_RE.search(text)
    assert m, f"{name}: could not locate the container `bash -c '...'` payload"
    return m.group(1)


@pytest.mark.parametrize("name", HARNESSES)
def test_no_unguarded_make_tail_pipeline(name):
    payload = inner_payload(name)
    piped = MAKE_TAIL_RE.findall(payload)
    if piped and not PIPEFAIL_RE.search(payload):
        joined = "\n".join(line.strip() for line in piped)
        pytest.fail(
            f"{name}: inner container shell pipes make through tail without "
            f"pipefail — a failed compile is reported as success:\n{joined}"
        )


@pytest.mark.parametrize("name", HARNESSES)
def test_every_make_invocation_is_fail_loud(name):
    payload = inner_payload(name)
    if PIPEFAIL_RE.search(payload):
        return  # pipefail in the same inner shell covers every pipeline
    bad = [
        m.group(1).strip()
        for m in MAKE_LINE_RE.finditer(payload)
        if not LOG_AND_CHECK_RE.search(m.group(1))
    ]
    if bad:
        joined = "\n".join(bad)
        pytest.fail(
            f"{name}: make invocation(s) neither run under inner-shell pipefail "
            f"nor use the log-and-check pattern (see scripts/graph_am_test.sh):\n"
            f"{joined}"
        )
