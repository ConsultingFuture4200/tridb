"""Pure-logic tests for wiki_reader's mutate-auth helpers (advisor plan 051).

No corpus, no HTTP server, no socket binding — exercises check_token / parse_body
directly. Runs under `make test`."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.wiki_reader import (  # noqa: E402
    MAX_BODY_BYTES,
    check_token,
    parse_body,
)


def test_check_token_matches_x_tridb_token_header():
    assert check_token({"X-TriDB-Token": "sekrit"}, "sekrit") is True


def test_check_token_matches_authorization_bearer_header():
    assert check_token({"Authorization": "Bearer sekrit"}, "sekrit") is True


def test_check_token_rejects_wrong_value():
    assert check_token({"X-TriDB-Token": "wrong"}, "sekrit") is False


def test_check_token_rejects_missing_header():
    assert check_token({}, "sekrit") is False


def test_check_token_rejects_when_no_expected_token_configured():
    # Fail closed even if a client somehow sends an empty token.
    assert check_token({"X-TriDB-Token": ""}, "") is False


def test_parse_body_parses_json():
    assert parse_body(b'{"a": 1}') == {"a": 1}


def test_parse_body_empty_is_empty_dict():
    assert parse_body(b"") == {}


def test_parse_body_rejects_oversized_payload():
    raw = b"x" * (MAX_BODY_BYTES + 1)
    with pytest.raises(ValueError):
        parse_body(raw)


def test_parse_body_accepts_payload_at_the_limit():
    raw = b'{"a":"' + b"x" * (MAX_BODY_BYTES - 10) + b'"}'
    assert len(raw) <= MAX_BODY_BYTES
    assert parse_body(raw)["a"].startswith("x")
