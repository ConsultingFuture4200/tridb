# graph_store — PGXS build. Build against the MSVBASE fork's pg_config:
#   make PG_CONFIG=/u01/app/postgres/product/13.4/bin/pg_config
#   make PG_CONFIG=... install
EXTENSION   = graph_store
DATA        = graph_store--0.1.0.sql
MODULE_big  = graph_store
OBJS        = graph_store.o
PG_CONFIG  ?= pg_config
PGXS       := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)
