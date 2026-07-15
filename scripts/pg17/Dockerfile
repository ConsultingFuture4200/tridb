# Stock PG 17 + pgvector + build toolchain for the un-forked TriDB graph AM (D2 phase 2.1).
# Base = the official pgvector image (stock Debian PG 17, 8KB BLCKSZ, pgvector preinstalled);
# we add PGXS build deps so src/graph_store compiles INSIDE the container against stock headers.
FROM pgvector/pgvector:pg17

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential postgresql-server-dev-17 \
    && rm -rf /var/lib/apt/lists/* \
    # let `make install` run as the postgres user (the test runner builds/installs
    # the extension inside a --user postgres container)
    && chown -R postgres:postgres \
        /usr/share/postgresql/17/extension \
        /usr/lib/postgresql/17/lib
