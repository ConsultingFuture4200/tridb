# PGXS build for the TriDB native graph-store access method (DEV-1164).
# Builds ONLY against the MSVBASE fork (PG 13.4, --with-blocksize=32). graphstore.h in this
# directory is the GX10 skeleton contract and is intentionally not compiled.
MODULE_big = graph_store_am
OBJS = graph_am.o
EXTENSION = graph_store_am
DATA = graph_store_am--0.2.0.sql graph_store_am--0.1.0--0.2.0.sql
PGFILEDESC = "graph_store_am - TriDB native adjacency-list graph store"

PG_CONFIG ?= pg_config
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)
