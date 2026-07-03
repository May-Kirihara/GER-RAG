# GaOTTT control plane (MV4)

An **independent** Postgres-backed package that acts as the aggregator /
audit / billing collection point for a multiverse deployment. It does **not**
depend on the `gaottt` engine package (design J9); the supervisor talks to it
over plain HTTP from the same host.

> Scope of this foundation (WP-1): schema, migration runner, asyncpg pool,
> config, and models. The FastAPI app (WP-2), `control_client` (WP-3), and
> supervisor integration (WP-4) are delivered in later work packages.

## Layout

```
control/
├── pyproject.toml          # independent package; asyncpg/fastapi/uvicorn/pydantic (no gaottt dep)
├── compose.yml             # disposable Postgres 16 (dev/test only)
├── control/
│   ├── __init__.py
│   ├── config.py           # ControlConfig (CONTROL_* env)
│   ├── db.py               # asyncpg pool create/close (fail-fast)
│   ├── migrate.py          # bootstrap-aware numbered SQL runner (1 file = 1 txn)
│   ├── models.py           # Pydantic v2 domain + request-body models
│   └── schema/
│       └── 001_initial.sql # 7 domain tables + default tenant bootstrap
└── tests/
    ├── conftest.py         # requires_postgres marker + disposable Postgres fixture
    ├── test_migrate.py     # pure-fn + DB-backed migration tests
    ├── test_models.py      # Pydantic validation (docker-free)
    └── test_db.py          # pool lifecycle (DB-backed)
```

The importable package is the inner `control/` directory; from the repository
root its file path is `control/control/...`, but in Python it is imported as
`control` (e.g. `from control.migrate import run_migrations`).

## Prerequisites

- Python 3.11+
- Docker (only for the `@pytest.mark.requires_postgres` tests)

## Install (editable, from the repository root)

```bash
uv pip install -e control/
# or: pip install -e control/
```

This pulls in `asyncpg`, `fastapi`, `uvicorn[standard]`, and `pydantic`. The
`dev` extra adds `pytest`, `pytest-asyncio`, `pytest-timeout`, and `httpx`:

```bash
uv pip install -e "control/[dev]"
```

## Start the disposable Postgres

```bash
docker compose -f control/compose.yml up -d
```

- Image: `postgres:16-alpine`
- Database / user / password: `gaottt_control` / `gaottt` / `dev-only`
- Host port: `55432` (avoids colliding with any host-local Postgres on 5432)
- **No volume** — `down` destroys all data. Disposable by design.

Tear down:

```bash
docker compose -f control/compose.yml down
```

The DSN for both the runner and the tests is:

```
postgresql://gaottt:dev-only@127.0.0.1:55432/gaottt_control
```

## Run migrations

```bash
export CONTROL_DATABASE_URL=postgresql://gaottt:dev-only@127.0.0.1:55432/gaottt_control
python -m control.migrate
```

The runner (`control.migrate`):

1. `ensure_bootstrap()` creates `schema_migrations(version TEXT PK, applied_at
   TIMESTAMPTZ NOT NULL DEFAULT now())` with `CREATE TABLE IF NOT EXISTS`
   **before** scanning numbered files (chicken-and-egg avoidance, Codex B7).
2. Lists `schema/NNN_*.sql` in numeric order and applies each pending file.
3. **One file = one transaction** — the file's SQL and the
   `schema_migrations` INSERT commit together, so a failure rolls the whole
   file back with no partial apply (atomicity guarantee).

`schema_migrations` is intentionally **not** defined inside `001_initial.sql`;
only the runner bootstraps it.

WP-2's FastAPI lifespan will call `ensure_bootstrap` + `run_migrations` on
startup, so manual `python -m control.migrate` is mainly for first-time setup
and debugging.

## Run the tests

```bash
cd control
python -m pytest tests/ -v
```

- The pure-function tests (`test_parse_version`, `test_list_migrations_sorted`,
  all of `test_models.py`) run with no docker.
- The `@pytest.mark.requires_postgres` tests start the disposable Postgres
  automatically and are **skipped** (not failed) when docker is unavailable.

To run only the docker-free tests:

```bash
python -m pytest tests/test_models.py \
  tests/test_migrate.py::test_parse_version \
  tests/test_migrate.py::test_list_migrations_sorted -v
```

### Overriding the disposable Postgres host port

`compose.yml` binds the disposable Postgres to host port **55432**. If that
port is already held on your machine (e.g. by another project's long-running
container), set `CONTROL_TEST_POSTGRES_HOST_PORT` to a free port and the test
suite will generate a standalone disposable compose file bound to that port
instead, under a unique compose project name so it never collides with any
other stack:

```bash
# Probe a free port, then:
export CONTROL_TEST_POSTGRES_HOST_PORT=55433
cd control
python -m pytest tests/ -v
```

The generated compose file lives in the system tempdir
(`gaottt-control-test-${PORT}.yml`) and is the sole `-f` input, so the chosen
port mapping fully *replaces* the default `55432:5432` (a plain compose
override would otherwise *union* the two `ports` lists and still try to bind
55432). Unset the variable to fall back to the default 55432.

## Configuration reference

`ControlConfig.from_env()` reads these variables (empty `admin_key` /
`database_url` are permitted at construction; WP-2 fails fast on them):

| env var                       | field              | default                   |
|-------------------------------|--------------------|---------------------------|
| `CONTROL_DATABASE_URL`        | `database_url`     | (empty)                   |
| `CONTROL_ADMIN_KEY`           | `admin_key`        | (empty)                   |
| `CONTROL_LISTEN_HOST`         | `listen_host`      | `127.0.0.1`               |
| `CONTROL_LISTEN_PORT`         | `listen_port`      | `7881`                    |
| `CONTROL_DB_POOL_MIN_SIZE`    | `db_pool_min_size` | `2`                       |
| `CONTROL_DB_POOL_MAX_SIZE`    | `db_pool_max_size` | `10`                      |
| `CONTROL_SCHEMA_DIR`          | `schema_dir`       | bundled `schema/`         |
