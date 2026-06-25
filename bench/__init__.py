"""TriDB benchmark package (DEV-1172 harness + DEV-1173 report).

Drives the ONE canonical query (spec §5) against both TriDB (via an engine
driver) and the multi-system baseline (``baseline/harness.py``) on an identical
corpus, captures success metrics SM-1..SM-5 per-query and in aggregate, and
renders a read-once HTML report comparing TriDB vs baseline against their
targets.

The live TriDB engine run is GX10/engine-gated. The engine is abstracted behind
``bench.driver.EngineDriver`` with a deterministic ``StubDriver`` so the whole
harness + metric capture + report path is runnable and unit-tested without the
engine.
"""
