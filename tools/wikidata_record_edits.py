"""Record a pinned Wikidata edit window from the Wikimedia EventStreams firehose.

Plan 060 / ADR-0018 — the hardware-independent recorder for Harness A
(bench/wikidata_consistency.py). ADR-0018 (a) pins the consistency measurement to "a
fixed recorded replay window" so a stranger re-running the harness replays the SAME
edits; this tool produces that window from the public recentchange SSE feed
(https://stream.wikimedia.org/v2/stream/recentchange).

FILTER. Only genuine item edits are usable cross-modal mutations: wiki ==
"wikidatawiki", namespace 0 (items), type "edit", title a plain item id "Q<int>".
Everything else on the shared firehose (other wikis, property/lexeme namespaces, page
creations, log events) is counted but not recorded.

OUTPUT. JSONL, one edit per line, matching bench/wikidata_consistency.parse_edit's
recorded-sample schema — {"entity": <Q int>, "rev": <revision.new int>} plus
informational fields ("ts", "user", "comment") the consistency model ignores. A
sidecar `<out>.meta.json` pins the window: first/last event timestamp, event and edit
counts, stream URL, recorded-at — the reproducibility pin ADR-0018 (a) requires.

RESILIENCE. Stdlib-only (urllib; SSE is just `data:`/`id:` lines). Malformed lines are
skipped; a dropped connection reconnects politely (Last-Event-ID resume, fixed backoff);
Ctrl-C flushes what it has and still writes the sidecar (every accepted edit is flushed
line-by-line, so nothing recorded is ever lost).

CLI:
    python -m tools.wikidata_record_edits --out data/wikidata_slice/edits.jsonl --edits 500
    python -m tools.wikidata_record_edits --out edits.jsonl --seconds 300
"""

from __future__ import annotations

import argparse
import http.client
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

from tools.wikidata_ingest import qid_to_int

RECORDER_VERSION = "0.1.0"
STREAM_URL = "https://stream.wikimedia.org/v2/stream/recentchange"
# Wikimedia asks automated clients to identify themselves.
USER_AGENT = f"tridb-plan060-edit-recorder/{RECORDER_VERSION} (DEV-1354 benchmark)"
FILTER_SPEC = 'wiki="wikidatawiki" namespace=0 type="edit" title="Q<int>"'


# ======================================================================================
# Pure parsing/filter layer (unit-tested, no network)
# ======================================================================================
def iter_sse_events(lines: Iterable[str]) -> Iterator[tuple[str | None, str]]:
    """Yield (event_id, data) per SSE event from an iterable of text lines.

    SSE framing: an event is a run of `field: value` lines terminated by a blank line;
    multiple `data:` lines concatenate with newlines; `:`-prefixed lines are comments
    (the feed's keepalives); an `id:` line carries the resume cursor the server accepts
    back as a Last-Event-ID header. Events with no data (pure keepalive/id frames) are
    not yielded.
    """
    event_id: str | None = None
    data_parts: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if data_parts:
                yield event_id, "\n".join(data_parts)
            event_id = None
            data_parts = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_parts.append(value)
        elif field == "id":
            event_id = value
    if data_parts:  # stream ended mid-event; the partial event is still usable
        yield event_id, "\n".join(data_parts)


def edit_from_change(obj: dict) -> dict | None:
    """One recentchange object -> a recorded-edit record, or None if not a usable edit.

    Usable == an item edit on wikidatawiki (see FILTER_SPEC) with an integer new
    revision. The record is the parse_edit schema ({"entity", "rev"}) plus the
    informational fields; entity is the Q-number int (qid_to_int, the ingest's rule).
    """
    if (
        obj.get("wiki") != "wikidatawiki"
        or obj.get("namespace") != 0
        or obj.get("type") != "edit"
    ):
        return None
    entity = qid_to_int(obj.get("title") or "")
    if entity is None:
        return None
    rev = (obj.get("revision") or {}).get("new")
    if not isinstance(rev, int):
        return None
    rec: dict = {"entity": entity, "rev": rev}
    for key, out_key in (("timestamp", "ts"), ("user", "user"), ("comment", "comment")):
        if key in obj:
            rec[out_key] = obj[key]
    return rec


@dataclass
class WindowStats:
    """Running window bounds/counters — everything the sidecar pin needs."""

    events: int = 0
    edits: int = 0
    first_ts: int | None = None
    last_ts: int | None = None
    last_event_id: str | None = None


def drain_stream(
    lines: Iterable[str],
    sink: Callable[[dict], None],
    stats: WindowStats,
    *,
    max_edits: int | None = None,
    deadline: float | None = None,
) -> bool:
    """Consume SSE lines, passing each usable edit to `sink`; update `stats`.

    Returns True when a stop condition (edit count / deadline) was hit — the window is
    complete — and False when the line stream simply ended (caller reconnects and
    resumes with stats.last_event_id). Malformed data lines are counted and skipped.
    """
    for event_id, data in iter_sse_events(lines):
        stats.events += 1
        if event_id is not None:
            stats.last_event_id = event_id
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            ts = obj.get("timestamp")
            if isinstance(ts, int):
                if stats.first_ts is None:
                    stats.first_ts = ts
                stats.last_ts = ts
            rec = edit_from_change(obj)
            if rec is not None:
                sink(rec)
                stats.edits += 1
                if max_edits is not None and stats.edits >= max_edits:
                    return True
        if deadline is not None and time.monotonic() >= deadline:
            return True
    return False


def window_meta(
    url: str,
    stats: WindowStats,
    *,
    max_edits: int | None,
    max_seconds: float | None,
    interrupted: bool = False,
) -> dict:
    """The sidecar pin: window bounds + provenance (ADR-0018 (a) reproducibility)."""
    return {
        "recorder": "tools/wikidata_record_edits.py",
        "recorder_version": RECORDER_VERSION,
        "stream_url": url,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "filter": FILTER_SPEC,
        "requested": {"edits": max_edits, "seconds": max_seconds},
        "events_seen": stats.events,
        "edits_recorded": stats.edits,
        "first_event_ts": stats.first_ts,
        "last_event_ts": stats.last_ts,
        "last_event_id": stats.last_event_id,
        "interrupted": interrupted,
        "schema": '{"entity": int (Q-number), "rev": int (revision.new), "ts"/"user"/'
        '"comment" informational} — bench/wikidata_consistency.parse_edit',
    }


# ======================================================================================
# Network loop (thin; everything above is the tested core)
# ======================================================================================
def _open_stream(url: str, last_event_id: str | None, timeout: float):
    headers = {"Accept": "text/event-stream", "User-Agent": USER_AGENT}
    if last_event_id is not None:
        headers["Last-Event-ID"] = last_event_id
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - pinned https URL


def _decode_lines(resp) -> Iterator[str]:
    for raw in resp:
        yield raw.decode("utf-8", errors="replace")


def record(
    url: str,
    out: Path,
    *,
    max_edits: int | None = None,
    max_seconds: float | None = None,
    retry_s: float = 5.0,
    read_timeout: float = 60.0,
) -> dict:
    """Record the window to `out` (JSONL) + `<out>.meta.json`. Returns the meta dict.

    Reconnects on any transport error, resuming via Last-Event-ID when one was seen.
    KeyboardInterrupt closes the window early but cleanly: every accepted edit was
    already flushed, and the sidecar is still written (marked interrupted).
    """
    stats = WindowStats()
    deadline = (time.monotonic() + max_seconds) if max_seconds else None
    interrupted = False
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:

        def sink(rec: dict) -> None:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()  # interrupt/crash never loses an accepted edit

        try:
            while True:
                if max_edits is not None and stats.edits >= max_edits:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                try:
                    resp = _open_stream(url, stats.last_event_id, read_timeout)
                except (urllib.error.URLError, OSError) as exc:
                    print(f"[record_edits] connect failed ({exc}); retry in {retry_s}s")
                    time.sleep(retry_s)
                    continue
                try:
                    done = drain_stream(
                        _decode_lines(resp),
                        sink,
                        stats,
                        max_edits=max_edits,
                        deadline=deadline,
                    )
                except (OSError, http.client.HTTPException) as exc:
                    print(f"[record_edits] stream dropped ({exc}); reconnecting")
                    done = False
                finally:
                    resp.close()
                if done:
                    break
                time.sleep(retry_s)  # polite backoff before re-dialing the feed
        except KeyboardInterrupt:
            interrupted = True
            print("[record_edits] interrupted — flushing window")
    meta = window_meta(
        url,
        stats,
        max_edits=max_edits,
        max_seconds=max_seconds,
        interrupted=interrupted,
    )
    meta_path = Path(str(out) + ".meta.json")
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(
        f"[record_edits] {stats.edits} edits / {stats.events} events -> {out} "
        f"(pin: {meta_path.name})"
    )
    return meta


# ======================================================================================
# CLI
# ======================================================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, required=True, help="output JSONL path")
    ap.add_argument("--edits", type=int, default=None, help="stop after N usable edits")
    ap.add_argument(
        "--seconds", type=float, default=None, help="stop after S seconds of window"
    )
    ap.add_argument("--url", type=str, default=STREAM_URL)
    ap.add_argument(
        "--retry", type=float, default=5.0, help="reconnect backoff (seconds)"
    )
    args = ap.parse_args(argv)
    if args.edits is None and args.seconds is None:
        ap.error("provide --edits and/or --seconds (else the window never closes)")
    if args.edits is not None and args.edits <= 0:
        ap.error("--edits must be positive")
    if args.seconds is not None and args.seconds <= 0:
        ap.error("--seconds must be positive")
    record(
        args.url,
        args.out,
        max_edits=args.edits,
        max_seconds=args.seconds,
        retry_s=args.retry,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
