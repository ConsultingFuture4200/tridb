# Stock PG + pgvector + build toolchain for the un-forked TriDB graph AM (D2 phase 2.1/2.4).
# Base = the official pgvector image (stock Debian PG, 8KB BLCKSZ, pgvector preinstalled);
# we add PGXS build deps so src/graph_store compiles INSIDE the container against stock headers.
# PG_MAJOR selects the Postgres major (16 and 17 are the roadmap 2.4 CI matrix).
ARG PG_MAJOR=17
ARG PGVECTOR_VERSION=0.8.0
FROM pgvector/pgvector:${PGVECTOR_VERSION}-pg${PG_MAJOR}
ARG PG_MAJOR

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential postgresql-server-dev-${PG_MAJOR} \
    && rm -rf /var/lib/apt/lists/* \
    # let `make install` run as the postgres user (the test runner builds/installs
    # the extension inside a --user postgres container)
    && chown -R postgres:postgres \
        /usr/share/postgresql/${PG_MAJOR}/extension \
        /usr/lib/postgresql/${PG_MAJOR}/lib
