"""Wiki-scale variant of the scripts/tridb_mcp_demo.sh session driver.

Points the stock MCP server (tools/tridb_mcp.py, unmodified) at a standing
release-image instance pre-loaded with the 200k-article enwiki slice
(14,686,050 hyperlink edges) as the memories corpus — the recording embedded
in the README was made with this script. Stores two personal memories, links
them to real Wikipedia articles (dense ids 9840 "Mushroom" and 10171
"Mycology" in the pinned slice), runs fused recall over the combined graph,
and prints the graph-lift diff vs pure-vector recall.

Prerequisites (see docs/mcp_agent_memory_v0.1.0.md, "The wiki demo corpus"):
a release-image container loaded via the plan-096 gate SQL's load section
(bench.wiki_ppr_gate --gen-sql, cut at #WPG LOAD_DONE) with the articles
table projected to the memories schema (kind/text columns + titles, table
and index renamed). Run from the repo root with TRIDB_DSN exported.
"""

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MEMORIES = [
    (
        "Mycofest 2026 is confirmed for the Pacific County farm; vendor applications open in March.",
        "note",
    ),
    ("Workshop idea: log-inoculation demo with shiitake dowels for beginners.", "task"),
]
# stored-memory index -> real Wikipedia article id (titles verified in the slice)
EDGES = [(0, 9840, "about"), (1, 10171, "about")]  # Mushroom, Mycology
QUERY = "what are our Mycofest plans?"


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

            stats0 = await call("memory_stats")
            print(
                f"corpus: {stats0['memories']:,} memories "
                f"(English Wikipedia articles), "
                f"{stats0['edges']:,} graph edges"
            )
            mids = []
            for text, kind in MEMORIES:
                mid = (await call("store_memory", text=text, kind=kind))["id"]
                mids.append(mid)
                print(f"stored [{mid}] ({kind}) {text}")
            for idx, dst, rel in EDGES:
                await call("connect", src_id=mids[idx], dst_id=dst, rel=rel)
                print(f"connected [{mids[idx]}] -{rel}-> [{dst}]")

            recall = await call("recall", query_text=QUERY, k=5, mode="fused")
            print(f'\nrecall(mode=fused, k=5): "{QUERY}"')
            for rank, r in enumerate(recall["results"], 1):
                print(f"  {rank}. [{r['id']}] ({r['kind']}) {r['text']}")
            print(
                f"graph_censored={recall['graph_censored']} "
                f"termination={recall['termination_reason']}"
            )
            vec = await call("recall", query_text=QUERY, k=5, mode="vector")
            injected = [
                r
                for r in recall["results"]
                if r["id"] not in {v["id"] for v in vec["results"]}
            ]
            for r in injected:
                print(
                    f'graph lift: [{r["id"]}] "{r["text"]}" is in fused top-5 but NOT '
                    f"vector top-5 — pulled in through the stored memory's edge"
                )

            stats = await call("memory_stats")
            print(
                f"\nstats: {stats['memories']:,} memories, {stats['edges']:,} edges, "
                f"extensions={stats['engine']['extensions']}"
            )

            got = [r["id"] for r in recall["results"]]
            if not got:
                raise SystemExit("FAIL: fused recall returned nothing")
    print("\nTRIDB MCP DEMO PASS (200k-article English Wikipedia corpus)")
    return 0


raise SystemExit(asyncio.run(main()))
