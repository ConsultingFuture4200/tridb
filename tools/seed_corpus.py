"""
Deterministic synthetic corpus generator for Omni RAG benchmark.

Generates entities with embeddings, edges, and queries for graph+vector+relational
benchmarking. All randomness is controlled by a single seed for deterministic output.
"""

import argparse
import os
import csv
import json
import numpy as np

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--entities', type=int, default=1000)
    parser.add_argument('--dim', type=int, default=768)
    parser.add_argument('--edges-per-node', type=int, default=8)
    parser.add_argument('--time-min', type=int, default=19000)
    parser.add_argument('--time-max', type=int, default=20000)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--out', default='data/seed/')
    
    args = parser.parse_args()
    
    os.makedirs(args.out, exist_ok=True)
    
    rng = np.random.default_rng(args.seed)
    
    # Generate entities
    entities = []
    for i in range(args.entities):
        timestamp = rng.integers(args.time_min, args.time_max + 1)
        chunk = f"chunk text for entity {i}"
        embedding = rng.standard_normal(args.dim).astype(np.float32)
        embedding /= np.linalg.norm(embedding)
        entities.append({
            'id': i,
            'timestamp': timestamp,
            'chunk': chunk,
            'embedding': embedding
        })
    
    # Write entities.csv
    with open(os.path.join(args.out, 'entities.csv'), 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['id', 'timestamp', 'chunk', 'embedding'])
        for e in entities:
            emb_str = '{' + ','.join(map(str, e['embedding'])) + '}'
            writer.writerow([e['id'], e['timestamp'], e['chunk'], emb_str])
    
    # Generate edges
    edges = []
    for i in range(args.entities):
        dsts = rng.choice(args.entities, args.edges_per_node, replace=False)
        dsts = [d for d in dsts if d != i]  # Remove self-loops
        for dst in dsts:
            edges.append({'src': i, 'dst': dst})
    
    # Write edges.csv
    with open(os.path.join(args.out, 'edges.csv'), 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['src', 'dst'])
        for e in edges:
            writer.writerow([e['src'], e['dst']])
    
    # Generate queries
    queries = []
    for qid in range(10):
        query_embedding = rng.standard_normal(args.dim).astype(np.float32)
        query_embedding /= np.linalg.norm(query_embedding)
        
        start_time = rng.integers(args.time_min, args.time_max - 29)
        selected_time_range = list(range(start_time, start_time + 30))
        
        queries.append({
            'qid': qid,
            'embedding': query_embedding.tolist(),
            'selected_time_range': selected_time_range
        })
    
    # Write queries.jsonl
    with open(os.path.join(args.out, 'queries.jsonl'), 'w') as f:
        for q in queries:
            f.write(json.dumps(q) + '\n')
    
    # Generate load.sql
    with open(os.path.join(args.out, 'load.sql'), 'w') as f:
        f.write("CREATE TABLE entity(id int primary key, timestamp int, chunk text, embedding float8[]);\n")
        f.write("CREATE TABLE related_to(src int, dst int);\n")
        f.write("\\copy entity(id,timestamp,chunk,embedding) FROM 'entities.csv' CSV HEADER;\n")
        f.write("\\copy related_to(src,dst) FROM 'edges.csv' CSV HEADER;\n")
    
    print(f"Wrote {args.entities} entities, {len(edges)} edges, 10 queries to {args.out}")

if __name__ == "__main__":
    main()
