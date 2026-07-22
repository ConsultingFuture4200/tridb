"""Animated terminal race for the demo GIF.

Replays MEASURED per-query latencies (race_data.json) as a real-time race:
each query's two bars grow concurrently, each completing after its measured
duration scaled by TIME_SCALE. All numbers shown are the measured values.
"""

import json
import sys
import time

CYAN = "\033[1;36m"
ORANGE = "\033[1;33m"
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[1;32m"
R = "\033[0m"

TIME_SCALE = 55.0  # 1 ms real -> 55 ms screen time
FPS = 12
BAR_MAX = 30  # columns for the slowest measured latency
LEFT_W = 55  # visible width of the left (TriDB) column


def cell(qn, ms, ms_max, prog):
    cols = max(1, round(ms * prog / ms_max * BAR_MAX))
    shown = f"{ms * prog:5.1f} ms" if prog < 1.0 else f"{ms:5.1f} ms"
    tick = " " if prog < 1.0 else ""
    return f"q{qn}  ", "▇" * cols, f" {shown}{tick}"


def row(qn, t_ms, b_ms, ms_max, tp, bp):
    lq, lb, ll = cell(qn, t_ms, ms_max, tp)
    rq, rb, rl = cell(qn, b_ms, ms_max, bp)
    lvis = len(lq) + len(lb) + len(ll)
    pad = " " * max(2, LEFT_W - lvis)
    lcol = f"  {DIM}{lq}{R}{CYAN}{lb}{R}{GREEN if tp >= 1 else DIM}{ll}{R}"
    rcol = f"{DIM}{rq}{R}{ORANGE}{rb}{R}{DIM if bp < 1 else ''}{rl}{R}"
    return lcol + pad + rcol


def frame(lines):
    sys.stdout.write("\033[H\033[2J" + "\n".join(lines) + "\n")
    sys.stdout.flush()


def main(path):
    d = json.load(open(path))
    qs = d["queries"][: d.get("show", 5)]
    ms_max = max(max(q["baseline_ms"], q["tridb_ms"]) for q in qs)
    header = [
        "",
        f"  {BOLD}the same fused query (vector seed + graph hop + rerank), recall 1.0 both sides —{R}",
        f"  {BOLD}200,000 Wikipedia articles · 14,686,050 real hyperlinks{R}",
        "",
        f"  {CYAN}{BOLD}TriDB — one Postgres process{R}"
        + " " * (LEFT_W - 28)
        + f"{ORANGE}{BOLD}Milvus + Neo4j + Postgres — app-side{R}",
        f"  {DIM}vector + graph + filter in one query plan{R}"
        + " " * (LEFT_W - 41)
        + f"{DIM}3 systems · 3 round-trips · merged in Python{R}",
        "",
    ]
    done = []
    for i, q in enumerate(qs, 1):
        t_ms, b_ms = q["tridb_ms"], q["baseline_ms"]
        t_end = max(t_ms * TIME_SCALE / 1000, 0.15)
        b_end = max(b_ms * TIME_SCALE / 1000, 0.15)
        t0 = time.perf_counter()
        while True:
            el = time.perf_counter() - t0
            tp, bp = min(el / t_end, 1.0), min(el / b_end, 1.0)
            frame(header + done + [row(i, t_ms, b_ms, ms_max, tp, bp)])
            if tp >= 1.0 and bp >= 1.0:
                done.append(row(i, t_ms, b_ms, ms_max, 1.0, 1.0))
                break
            time.sleep(1.0 / FPS)
        time.sleep(0.3)
    tm, bm = d["tridb_median_ms"], d["baseline_median_ms"]
    h2 = d.get("hop2")
    end = (
        header
        + done
        + [
            "",
            f"  {BOLD}median  {CYAN}{tm:.1f} ms{R}{BOLD}  vs  {ORANGE}{bm:.1f} ms{R}"
            f"{BOLD}   →   {GREEN}{bm / tm:.1f}× faster, identical answers{R}",
        ]
    )
    if h2:
        t2, b2 = h2["tridb_median_ms"], h2["baseline_median_ms"]
        end.append(
            f"  {BOLD}2-hop   {CYAN}{t2:.0f} ms{R}{BOLD}  vs  {ORANGE}{b2:.0f} ms{R}"
            f"{BOLD}   →   {GREEN}{b2 / t2:.1f}×{R}"
        )
    end += [
        "",
        f"  {DIM}measured live, warm, client-clocked; all-localhost = the multi-store's BEST case{R}",
        f"  {DIM}method + full tables: docs/benchmark_wiki_fusion_v0.1.0.md · github.com/ConsultingFuture4200/tridb{R}",
    ]
    frame(end)
    time.sleep(2.5)


if __name__ == "__main__":
    main(sys.argv[1])
