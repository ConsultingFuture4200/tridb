"""Pure-logic tests for wiki_reader's mutate-auth helpers (advisor plans 051 + 081).

No corpus, no HTTP server, no socket binding — exercises check_token / parse_body,
the bind-mode + operator-token helpers, the server-canonical pending-suggestion
store, and the accept/dismiss revalidation boundary directly. Runs under
`make test`."""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tools.wiki_reader as wr  # noqa: E402
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


# --------------------------------------------------------------------------- #
# Advisor plan 081 — bind mode + operator token (test fixture values only)
# --------------------------------------------------------------------------- #

STRONG_TOKEN = "test-operator-token-abcdef123456"  # >= 16 chars, fixture only


def test_is_loopback_host_accepts_loopback_addresses():
    for h in ("127.0.0.1", "127.0.0.2", "::1", "localhost"):
        assert wr.is_loopback_host(h) is True


def test_is_loopback_host_rejects_non_loopback():
    for h in ("0.0.0.0", "192.168.1.5", "10.0.0.1", "example.com", ""):
        assert wr.is_loopback_host(h) is False


def test_resolve_token_loopback_generates_ephemeral_session_token():
    tok, generated = wr.resolve_operator_token("127.0.0.1", environ={})
    assert generated is True
    assert len(tok) >= wr.MIN_OPERATOR_TOKEN_CHARS


def test_resolve_token_loopback_prefers_configured_token():
    tok, generated = wr.resolve_operator_token(
        "127.0.0.1", environ={"WIKI_READER_OPERATOR_TOKEN": STRONG_TOKEN}
    )
    assert (tok, generated) == (STRONG_TOKEN, False)


def test_resolve_token_remote_requires_token():
    with pytest.raises(ValueError, match="WIKI_READER_OPERATOR_TOKEN"):
        wr.resolve_operator_token("0.0.0.0", environ={})


def test_resolve_token_remote_rejects_weak_token():
    with pytest.raises(ValueError, match="too short"):
        wr.resolve_operator_token(
            "0.0.0.0", environ={"WIKI_READER_OPERATOR_TOKEN": "short"}
        )


def test_resolve_token_remote_accepts_strong_token():
    tok, generated = wr.resolve_operator_token(
        "0.0.0.0", environ={"WIKI_READER_OPERATOR_TOKEN": STRONG_TOKEN}
    )
    assert (tok, generated) == (STRONG_TOKEN, False)


def test_check_token_constant_time_compare_handles_non_ascii():
    assert check_token({"X-TriDB-Token": "tokén"}, "tokén") is True
    assert check_token({"X-TriDB-Token": "tokén"}, "other") is False


def test_remote_index_html_never_contains_operator_token():
    page = wr.render_index_html(STRONG_TOKEN, embed_token=False)
    assert STRONG_TOKEN not in page
    assert '<meta name="wr-token" content="">' in page


def test_loopback_index_html_embeds_session_token():
    page = wr.render_index_html("local-session-tok-1234", embed_token=True)
    assert 'content="local-session-tok-1234"' in page


# --------------------------------------------------------------------------- #
# Advisor plan 081 — server-canonical pending-suggestion store
# --------------------------------------------------------------------------- #


def _register(
    store,
    subject_id=1,
    source_id=2,
    prop="born",
    value="1912",
    title="Article 2",
    snippet="was born in 1912 in London",
):
    return store.register(
        subject_id=subject_id,
        source_id=source_id,
        prop=prop,
        value=value,
        source_title=title,
        source_snippet=snippet,
    )


def test_pending_claim_returns_canonical_entry():
    s = wr.PendingSuggestions()
    sug = _register(s)
    status, e = s.claim(sug, 1)
    assert status == "ok"
    assert (e["property"], e["value"], e["source_id"]) == ("born", "1912", 2)


def test_pending_unknown_id_rejected():
    assert wr.PendingSuggestions().claim("nope", 1) == ("unknown", None)


def test_pending_subject_mismatch_rejected():
    s = wr.PendingSuggestions()
    sug = _register(s, subject_id=1)
    assert s.claim(sug, 99) == ("unknown", None)


def test_pending_entry_expires():
    now = [0.0]
    s = wr.PendingSuggestions(ttl_s=10, clock=lambda: now[0])
    sug = _register(s)
    now[0] = 11.0
    assert s.claim(sug, 1) == ("unknown", None)


def test_pending_capacity_evicts_oldest():
    s = wr.PendingSuggestions(max_entries=3)
    ids = [_register(s, value=str(i)) for i in range(4)]
    assert s.claim(ids[0], 1) == ("unknown", None)  # oldest evicted
    assert s.claim(ids[3], 1)[0] == "ok"


def test_pending_claim_is_single_use_and_concurrent_safe():
    s = wr.PendingSuggestions()
    sug = _register(s)
    wins: list[dict] = []
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        status, e = s.claim(sug, 1)
        if status == "ok":
            wins.append(e)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1
    s.consume(sug)
    assert s.claim(sug, 1) == ("unknown", None)  # replay after consumption


def test_pending_inflight_claim_reports_replay_and_release_restores():
    s = wr.PendingSuggestions()
    sug = _register(s)
    assert s.claim(sug, 1)[0] == "ok"
    assert s.claim(sug, 1) == ("replay", None)
    s.release(sug)
    assert s.claim(sug, 1)[0] == "ok"


# --------------------------------------------------------------------------- #
# Advisor plan 081 — accept/dismiss revalidate server-side, by id only
# --------------------------------------------------------------------------- #

SRC_BODY = "Alan Turing was born in 1912 in London. He studied at Cambridge."


def _bare_reader():
    r = wr.Reader.__new__(wr.Reader)
    r.overlay = sqlite3.connect(":memory:", check_same_thread=False)
    r.overlay_lock = threading.Lock()
    r.pending = wr.PendingSuggestions()
    r._init_overlay()
    r.article = lambda aid, with_html=False: {
        "id": aid,
        "title": f"Article {aid}",
        "body": SRC_BODY,
    }
    return r


def test_accept_persists_canonical_fields_once():
    r = _bare_reader()
    sug = _register(r.pending)
    code, obj = r.accept_fact({"subject_id": 1, "suggestion_id": sug})
    assert code == 200 and obj["ok"] is True
    facts = r.overlay_facts(1)
    assert len(facts) == 1
    f = facts[0]
    assert (f["property"], f["value"], f["source_id"]) == ("born", "1912", 2)
    assert f["source_title"] == "Article 2"  # derived server-side from the reload
    assert f["source_snippet"] == "was born in 1912 in London"
    # replay: the id was consumed by the successful write
    assert r.accept_fact({"subject_id": 1, "suggestion_id": sug})[0] == 400
    assert len(r.overlay_facts(1)) == 1


def test_accept_ignores_client_supplied_fact_fields():
    r = _bare_reader()
    sug = _register(r.pending)
    code, _ = r.accept_fact(
        {
            "subject_id": 1,
            "suggestion_id": sug,
            "property": "EVIL",
            "value": "FORGED",
            "source_id": 999,
            "source_title": "Fake",
            "source_snippet": "fabricated",
        }
    )
    assert code == 200
    f = r.overlay_facts(1)[0]
    assert (f["property"], f["value"], f["source_id"]) == ("born", "1912", 2)


def test_accept_rejects_when_source_text_changed():
    r = _bare_reader()
    sug = _register(r.pending)
    r.article = lambda aid, with_html=False: {
        "id": aid,
        "title": "Article",
        "body": "Completely rewritten text.",
    }
    code, _ = r.accept_fact({"subject_id": 1, "suggestion_id": sug})
    assert code == 409
    assert r.overlay_facts(1) == []


def test_accept_rejects_missing_source_article():
    r = _bare_reader()
    sug = _register(r.pending)
    r.article = lambda aid, with_html=False: None
    code, _ = r.accept_fact({"subject_id": 1, "suggestion_id": sug})
    assert code == 409
    assert r.overlay_facts(1) == []


def test_accept_rejects_unknown_mismatched_and_malformed():
    r = _bare_reader()
    assert r.accept_fact({"subject_id": 1, "suggestion_id": "nope"})[0] == 400
    sug = _register(r.pending, subject_id=1)
    assert r.accept_fact({"subject_id": 7, "suggestion_id": sug})[0] == 400
    assert r.accept_fact({})[0] == 400
    assert r.overlay_facts(1) == []


def test_accept_rejects_disallowed_property():
    r = _bare_reader()
    sug = _register(r.pending, prop="bad\x00prop")
    code, _ = r.accept_fact({"subject_id": 1, "suggestion_id": sug})
    assert code == 400
    assert r.overlay_facts(1) == []


def test_dismiss_consumes_by_id_with_canonical_fields():
    r = _bare_reader()
    sug = _register(r.pending)
    code, obj = r.dismiss_fact(
        {
            "subject_id": 1,
            "suggestion_id": sug,
            "property": "EVIL",
            "value": "FORGED",  # client fields must be ignored
        }
    )
    assert code == 200 and obj["ok"] is True
    rows = r.overlay.execute(
        "SELECT subject_id, property, value FROM dismissed"
    ).fetchall()
    assert rows == [(1, "born", "1912")]
    assert r.dismiss_fact({"subject_id": 1, "suggestion_id": sug})[0] == 400


def test_dismiss_unknown_id_rejected():
    r = _bare_reader()
    assert r.dismiss_fact({"subject_id": 1, "suggestion_id": "nope"})[0] == 400


def test_enrich_registers_canonical_suggestions_with_opaque_ids():
    r = _bare_reader()

    def article(aid, with_html=False):
        if aid == 1:
            return {"id": 1, "title": "Alan Turing", "body": "A mathematician."}
        return {"id": aid, "title": f"Article {aid}", "body": SRC_BODY}

    r.article = article
    r.semantic = lambda aid, k=8: []
    r._enrich_source_ids = lambda aid, sem: [2]
    r._enrich_missing_sections = lambda aid, target, sem: []
    r._enrich_extract = lambda target, sources: [
        {
            "property": "born",
            "value": "1912",
            "source_id": 2,
            "source_snippet": "was born in 1912 in London",
        }
    ]
    out = r.enrich(1)
    assert len(out["suggestions"]) == 1
    sug = out["suggestions"][0]
    assert sug["suggestion_id"]
    status, entry = r.pending.claim(sug["suggestion_id"], 1)
    assert status == "ok" and entry["property"] == "born"


# --------------------------------------------------------------------------- #
# Advisor plan 081 — handler-level authorization (no sockets: __new__ pattern)
# --------------------------------------------------------------------------- #


class _StubReader:
    def __init__(self):
        self.calls: list[dict] = []

    def accept_fact(self, d):
        self.calls.append(d)
        return 200, {"ok": True, "facts": []}

    def dismiss_fact(self, d):
        self.calls.append(d)
        return 200, {"ok": True}


def _post(handler_cls, path, payload, headers):
    h = handler_cls.__new__(handler_cls)
    raw = json.dumps(payload).encode()
    h.headers = {"Content-Length": str(len(raw)), **headers}
    h.rfile = io.BytesIO(raw)
    h.wfile = io.BytesIO()
    h.path = path
    h.command = "POST"
    h.request_version = "HTTP/1.1"
    h.requestline = f"POST {path} HTTP/1.1"
    h.client_address = ("127.0.0.1", 0)
    h.do_POST()
    return int(h.wfile.getvalue().split(b" ", 2)[1])


def test_post_missing_token_gets_403():
    stub = _StubReader()
    handler = wr.make_handler(stub, STRONG_TOKEN, embed_token=False)
    body = {"subject_id": 1, "suggestion_id": "x"}
    assert _post(handler, "/enrich/accept", body, {}) == 403
    assert stub.calls == []


def test_post_wrong_token_gets_403():
    stub = _StubReader()
    handler = wr.make_handler(stub, STRONG_TOKEN, embed_token=False)
    body = {"subject_id": 1, "suggestion_id": "x"}
    assert _post(handler, "/enrich/dismiss", body, {"X-TriDB-Token": "wrong"}) == 403
    assert stub.calls == []


def test_post_correct_token_dispatches():
    stub = _StubReader()
    handler = wr.make_handler(stub, STRONG_TOKEN, embed_token=False)
    body = {"subject_id": 1, "suggestion_id": "x"}
    code = _post(handler, "/enrich/accept", body, {"X-TriDB-Token": STRONG_TOKEN})
    assert code == 200
    assert stub.calls == [body]
