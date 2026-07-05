"""Render a k-hop article neighborhood from Neo4j as a self-contained HTML graph.

DEV-1354 visual track, Community-compatible path. Given a seed article title, this
queries the LINKS_TO neighborhood loaded by tools/wiki_neo4j_load.py and:

  (a) prints the node/edge sample + a Neo4j Browser Cypher recipe you can paste
      into http://localhost:7474 (which renders the graph natively), and
  (b) writes a SELF-CONTAINED, CDN-free interactive HTML file (default
      bench/out/wiki_subgraph.html) with an inlined zero-dependency force-directed
      canvas renderer — drag nodes, hover for titles, seed highlighted. No network
      access at view time, so it works fully offline.

Neighborhood query uses APOC (apoc.path.subgraphAll — enabled in the baseline
image) for a clean induced subgraph; if APOC is unavailable it falls back to a
plain variable-length Cypher match.

--- Upgrade path to Neo4j Bloom (honest note) ---------------------------------
Bloom is the polished, no-code graph-exploration UI, but it is NOT in Neo4j
Community (the baseline image here). To get Bloom you need Neo4j Desktop (free,
local — bundles Bloom against a local DB) or Enterprise/Aura. Once on Desktop,
point Bloom at this same DB, add a perspective on the :Article label with :LINKS_TO
and search a title to explore interactively. This tool is the Community-tier
substitute: the Browser recipe + the offline HTML give the same "see the
neighborhood" capability without the Bloom license.

Usage:
    python -m tools.wiki_subgraph --seed April --hops 2 --limit 150
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from neo4j import GraphDatabase

SUBGRAPH_APOC = """
MATCH (seed:Article {title: $seed})
CALL apoc.path.subgraphAll(seed, {
    maxLevel: $hops,
    relationshipFilter: 'LINKS_TO',
    limit: $limit
}) YIELD nodes, relationships
RETURN
    [n IN nodes | {id: n.id, title: n.title}] AS nodes,
    [r IN relationships | {src: startNode(r).id, dst: endNode(r).id}] AS rels
"""

# Plain-Cypher fallback (no APOC). ``hops`` is interpolated as a validated int
# because Cypher forbids a parameter in the *range* of a variable-length pattern.
SUBGRAPH_PLAIN = """
MATCH (seed:Article {{title: $seed}})
MATCH p = (seed)-[:LINKS_TO*1..{hops}]-(m:Article)
WITH seed, m, relationships(p) AS rels
LIMIT $limit
UNWIND rels AS r
WITH collect(DISTINCT seed) + collect(DISTINCT m) AS ns,
     collect(DISTINCT r) AS rs
RETURN
    [n IN ns | {{id: n.id, title: n.title}}] AS nodes,
    [r IN rs | {{src: startNode(r).id, dst: endNode(r).id}}] AS rels
"""


def browser_recipe(seed: str, hops: int, limit: int) -> str:
    """A copy-paste Neo4j Browser query — Browser renders the returned graph natively."""
    return (
        f"MATCH p = (seed:Article {{title: '{seed}'}})"
        f"-[:LINKS_TO*1..{hops}]-(:Article)\n"
        f"RETURN p LIMIT {limit};"
    )


def fetch_subgraph(
    session, seed: str, hops: int, limit: int
) -> tuple[list[dict], list[dict]]:
    try:
        rec = session.run(SUBGRAPH_APOC, seed=seed, hops=hops, limit=limit).single()
    except Exception as exc:  # APOC missing / procedure not found -> plain fallback
        print(f"[apoc unavailable: {exc}; using plain Cypher fallback]")
        rec = session.run(
            SUBGRAPH_PLAIN.format(hops=hops), seed=seed, limit=limit
        ).single()
    if rec is None:
        return [], []
    return rec["nodes"], rec["rels"]


def render_html(
    seed: str, hops: int, nodes: list[dict], rels: list[dict], out_path: Path
) -> None:
    """Write a self-contained interactive HTML (inlined canvas force layout)."""
    payload = json.dumps({"seed": seed, "nodes": nodes, "rels": rels})
    title = html.escape(f"TriDB wiki subgraph — {seed} ({hops}-hop)")
    doc = _HTML_TEMPLATE.replace("__TITLE__", title).replace("__PAYLOAD__", payload)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", required=True, help="article title to seed from")
    ap.add_argument("--hops", type=int, default=2, help="neighborhood radius")
    ap.add_argument("--limit", type=int, default=150, help="max nodes in subgraph")
    ap.add_argument("--out", type=Path, default=Path("bench/out/wiki_subgraph.html"))
    ap.add_argument("--uri", default="bolt://localhost:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="testpassword")
    ap.add_argument("--database", default="neo4j")
    args = ap.parse_args(argv)

    if args.hops < 1:
        print("--hops must be >= 1")
        return 1

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    try:
        driver.verify_connectivity()
        with driver.session(database=args.database) as session:
            nodes, rels = fetch_subgraph(session, args.seed, args.hops, args.limit)
    finally:
        driver.close()

    if not nodes:
        print(f"no neighborhood for seed '{args.seed}' — is it loaded / spelled right?")
        return 1

    print(
        f"seed '{args.seed}': {len(nodes)} nodes, {len(rels)} edges (<= {args.hops} hops)"
    )
    print("\nsample nodes:")
    for n in nodes[:12]:
        print(f"  {n['id']:>7}  {n['title']}")
    if len(nodes) > 12:
        print(f"  ... (+{len(nodes) - 12} more)")

    print("\nNeo4j Browser recipe (paste at http://localhost:7474):")
    print("  " + browser_recipe(args.seed, args.hops, args.limit).replace("\n", "\n  "))

    render_html(args.seed, args.hops, nodes, rels, args.out)
    print(f"\nwrote self-contained interactive HTML -> {args.out}")
    return 0


# --- inlined, CDN-free viewer -------------------------------------------------
# A compact canvas force-directed renderer (repulsion + spring + centering).
# Drag nodes, hover for the article title, seed node highlighted. No external
# scripts/styles/fonts, so the file opens offline.
_HTML_TEMPLATE = """<div id="wrap">
<h1>__TITLE__</h1>
<p id="meta"></p>
<canvas id="c"></canvas>
<p class="hint">drag a node to pin it &middot; hover for the title &middot; scroll area is the neighborhood loaded from Neo4j</p>
</div>
<style>
  #wrap { font-family: system-ui, sans-serif; margin: 0; padding: 16px; color: #1a1a2e; }
  h1 { font-size: 16px; margin: 0 0 4px; }
  #meta, .hint { font-size: 12px; color: #555; margin: 2px 0 10px; }
  canvas { border: 1px solid #d0d0dd; border-radius: 6px; background: #fbfbfe; display: block; width: 100%; max-width: 100%; touch-action: none; }
</style>
<script>
const DATA = __PAYLOAD__;
const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d');
function size() { canvas.width = canvas.clientWidth; canvas.height = 560; }
size(); window.addEventListener('resize', () => { size(); });

const idset = new Set(DATA.nodes.map(n => n.id));
const nodes = DATA.nodes.map(n => ({
  id: n.id, title: n.title || String(n.id),
  x: canvas.width/2 + (Math.random()-0.5)*300,
  y: canvas.height/2 + (Math.random()-0.5)*300,
  vx: 0, vy: 0, seed: n.title === DATA.seed, pin: false
}));
const byId = new Map(nodes.map(n => [n.id, n]));
const edges = DATA.rels.filter(r => byId.has(r.src) && byId.has(r.dst))
  .map(r => ({a: byId.get(r.src), b: byId.get(r.dst)}));
document.getElementById('meta').textContent =
  nodes.length + ' articles, ' + edges.length + ' LINKS_TO edges';

const K_REP = 4200, K_SPR = 0.012, REST = 90, CENTER = 0.006, DAMP = 0.86;
function step() {
  for (let i=0;i<nodes.length;i++){
    const a=nodes[i];
    for (let j=i+1;j<nodes.length;j++){
      const b=nodes[j];
      let dx=a.x-b.x, dy=a.y-b.y, d2=dx*dx+dy*dy+0.01, d=Math.sqrt(d2);
      const f=K_REP/d2, fx=f*dx/d, fy=f*dy/d;
      a.vx+=fx; a.vy+=fy; b.vx-=fx; b.vy-=fy;
    }
  }
  for (const e of edges){
    let dx=e.b.x-e.a.x, dy=e.b.y-e.a.y, d=Math.sqrt(dx*dx+dy*dy)+0.01;
    const f=K_SPR*(d-REST), fx=f*dx/d, fy=f*dy/d;
    e.a.vx+=fx; e.a.vy+=fy; e.b.vx-=fx; e.b.vy-=fy;
  }
  const cx=canvas.width/2, cy=canvas.height/2;
  for (const n of nodes){
    n.vx+=(cx-n.x)*CENTER; n.vy+=(cy-n.y)*CENTER;
    if (n.pin){ n.vx=0; n.vy=0; continue; }
    n.vx*=DAMP; n.vy*=DAMP;
    n.x+=n.vx; n.y+=n.vy;
    n.x=Math.max(14,Math.min(canvas.width-14,n.x));
    n.y=Math.max(14,Math.min(canvas.height-14,n.y));
  }
}
let hover=null;
function draw(){
  ctx.clearRect(0,0,canvas.width,canvas.height);
  ctx.strokeStyle='rgba(120,120,160,0.35)'; ctx.lineWidth=1;
  for (const e of edges){ ctx.beginPath(); ctx.moveTo(e.a.x,e.a.y); ctx.lineTo(e.b.x,e.b.y); ctx.stroke(); }
  for (const n of nodes){
    const r=n.seed?9:5;
    ctx.beginPath(); ctx.arc(n.x,n.y,r,0,6.2832);
    ctx.fillStyle=n.seed?'#e63946':(n===hover?'#457b9d':'#5a67d8');
    ctx.fill();
    if (n.seed || n===hover){
      ctx.fillStyle='#1a1a2e'; ctx.font='12px system-ui,sans-serif';
      ctx.fillText(n.title, n.x+8, n.y-8);
    }
  }
}
function loop(){ step(); draw(); requestAnimationFrame(loop); }
loop();

function at(mx,my){
  let best=null, bd=225;
  for (const n of nodes){ const d=(n.x-mx)**2+(n.y-my)**2; if(d<bd){bd=d;best=n;} }
  return best;
}
let drag=null;
function pos(ev){ const r=canvas.getBoundingClientRect(); return [ev.clientX-r.left, ev.clientY-r.top]; }
canvas.addEventListener('mousemove', ev=>{ const [x,y]=pos(ev);
  if (drag){ drag.x=x; drag.y=y; drag.pin=true; } else { hover=at(x,y); }
  canvas.style.cursor=(hover||drag)?'pointer':'default';
});
canvas.addEventListener('mousedown', ev=>{ const [x,y]=pos(ev); drag=at(x,y); if(drag) drag.pin=true; });
window.addEventListener('mouseup', ()=>{ drag=null; });
</script>
"""


if __name__ == "__main__":
    raise SystemExit(main())
