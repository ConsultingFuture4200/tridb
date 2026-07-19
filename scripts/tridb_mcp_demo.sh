#!/usr/bin/env bash
#
# tridb_mcp_demo.sh — agent memory in one docker run (advisor plan 098).
# Starts the SHIPPED release image (tridb/postgres-trimodal:pg17), bootstraps
# the memory schema, then drives a real store/connect/recall session through
# tools/tridb_mcp.py over its actual stdio JSON-RPC transport (the mcp client
# library — the same wire an MCP-capable agent uses), prints the fused-recall
# result, and cleans up. Embeddings are real (fastembed BGE-small, CPU).
#
# Usage: scripts/tridb_mcp_demo.sh [image]      (default tridb/postgres-trimodal:pg17)
# Needs: docker, the release image, and `pip install -r requirements-mcp.txt`.
#
# Lifecycle safety (pg17_release_smoke.sh discipline): container name/password
# generated per run, host port picked by docker (127.0.0.1 ephemeral), container
# always removed on exit — success or failure.
set -euo pipefail

IMAGE="${1:-tridb/postgres-trimodal:pg17}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)"

docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "image $IMAGE not built — run: make stock-release-smoke PG_MAJOR=17" >&2
  exit 1
}
"$PY" -c 'import mcp' 2>/dev/null || {
  echo "mcp package missing — run: pip install -r requirements-mcp.txt" >&2
  exit 1
}

SUFFIX="$(od -An -N4 -tx4 /dev/urandom | tr -d ' ')"
NAME="tridb-mcp-demo-$SUFFIX"
PW="$(od -An -N16 -tx8 /dev/urandom | tr -d ' \n')"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --name "$NAME" -e POSTGRES_PASSWORD="$PW" \
  -p 127.0.0.1:0:5432 "$IMAGE" >/dev/null

# Readiness: two consecutive OK probes 1s apart (initdb's temp server races one).
ok=0
for _ in $(seq 1 60); do
  if docker exec "$NAME" pg_isready -U postgres >/dev/null 2>&1; then
    ok=$((ok + 1))
    [ "$ok" -ge 2 ] && break
  else
    ok=0
  fi
  sleep 1
done
if [ "$ok" -lt 2 ]; then
  echo "FAIL: $IMAGE not ready after 60s" >&2
  docker logs "$NAME" 2>&1 | tail -20 >&2
  exit 1
fi

PORT="$(docker port "$NAME" 5432/tcp | head -1 | sed 's/.*://')"
export TRIDB_DSN="postgresql://postgres:$PW@127.0.0.1:$PORT/postgres"

"$PY" -m tools.tridb_mcp --init

"$PY" - <<'EOF'
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MEMORIES = [
    ("Mycofest 2026 venue decision: the festival is on the Pacific County farm.", "fact"),
    ("The extraction lab needs a new rotary evaporator before spring.", "task"),
    ("The north pasture hosts the Mycofest main stage.", "fact"),
    ("Festival parking overflows onto the county road; shuttle needed.", "note"),
    ("STATE gummies use fermented fruit puree from CDE Ingredients.", "fact"),
]
# venue(0) -> stage(2) -> parking(3): the connected event cluster.
EDGES = [(0, 2, "details"), (2, 3, "details")]
QUERY = "where is the festival happening"


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "tools.tridb_mcp"], env=dict(os.environ)
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as sess:
            await sess.initialize()

            async def call(name, **kw):
                res = await sess.call_tool(name, kw)
                if res.isError:
                    raise SystemExit(f"tool {name} failed: {res.content[0].text}")
                return json.loads(res.content[0].text)

            listed = sorted(t.name for t in (await sess.list_tools()).tools)
            print(f"tools: {listed}")
            for text, kind in MEMORIES:
                mid = (await call("store_memory", text=text, kind=kind))["id"]
                print(f"stored [{mid}] ({kind}) {text}")
            for src, dst, rel in EDGES:
                await call("connect", src_id=src, dst_id=dst, rel=rel)
                print(f"connected {src} -{rel}-> {dst}")

            recall = await call("recall", query_text=QUERY, k=3, mode="fused")
            print(f'\nrecall(mode=fused, k=3): "{QUERY}"')
            for rank, r in enumerate(recall["results"], 1):
                print(f'  {rank}. [{r["id"]}] score={r["score"]:.4f} {r["text"]}')
            print(
                f'graph_censored={recall["graph_censored"]} '
                f'termination={recall["termination_reason"]}'
            )

            stats = await call("memory_stats")
            print(
                f'\nstats: {stats["memories"]} memories, {stats["edges"]} edges, '
                f'extensions={stats["engine"]["extensions"]}'
            )

            got = [r["id"] for r in recall["results"]]
            if not got:
                raise SystemExit("FAIL: fused recall returned nothing")
            if 0 not in got:
                raise SystemExit(f"FAIL: venue memory (id 0) not in top-3: {got}")
            if stats["memories"] != 5 or stats["edges"] != 2:
                raise SystemExit(f"FAIL: stats wrong: {stats}")
    print("\nTRIDB MCP DEMO PASS")
    return 0


raise SystemExit(asyncio.run(main()))
EOF
