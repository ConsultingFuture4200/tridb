# PGXS build for the stock-PG fused tri-modal operator (ADR-0019, D2 phase 2.5).
# Requires pgvector (vector type) + graph_store_am at CREATE EXTENSION time; compiles
# against stock PG 16/17 headers only.
MODULE_big = tjs_pg
OBJS = tjs_pg.o
EXTENSION = tjs_pg
DATA = tjs_pg--0.1.0.sql
PGFILEDESC = "tjs_pg - TriDB fused tri-modal operator on stock PostgreSQL"

PG_CONFIG ?= pg_config
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)
