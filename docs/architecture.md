# MeowPay: schema, persistence and the HTTP shell

Navigational map of what exists. Rationale for what was chosen and what was
skipped lives in [decisions.md](decisions.md).

A cat is both the user and the account holder. Humans are not modelled: a human
is a funding source outside the system boundary, so a top-up is a deposit from a
system treasury account rather than a movement between two stored entities.

---

## Repo layout

```
meowpay/
├── .gitattributes
├── .gitignore
├── .pre-commit-config.yaml
├── LICENSE
├── Makefile
├── README.md
│
├── docs/
│   ├── architecture.md              # this file
│   └── decisions.md                 # what was chosen, what was skipped, why
│
└── backend/
    ├── .env.example                 # canonical variable list. .env is gitignored
    ├── .python-version
    ├── alembic.ini                  # no sqlalchemy.url: migrations read it from config
    ├── pyproject.toml
    ├── uv.lock
    │
    ├── migrations/
    │   ├── env.py
    │   ├── script.py.mako
    │   └── versions/
    │       ├── 0001_create_cats_transfers_and_entries.py
    │       └── 0002_insert_the_treasury_cat.py
    │
    ├── src/meowpay/
    │   ├── config.py
    │   ├── constants.py
    │   ├── db.py
    │   ├── models.py
    │   ├── session.py
    │   └── api/
    │       ├── app.py
    │       ├── deps.py
    │       ├── middleware.py
    │       ├── schemas.py
    │       └── routes/health.py
    │
    └── tests/
        ├── conftest.py
        ├── test_health.py
        └── test_schema.py
```

---

## Modules (`backend/src/meowpay/`)

| Module | Responsibility |
|---|---|
| `config.py` | Env loading from `backend/.env`. `require_env()` raises at the point of use so a missing variable names itself in the traceback of whatever needed it; `optional_env()` treats empty as unset. `database_url()` does not return what it was given: it rewrites a bare `postgresql://` to `postgresql+psycopg` (SQLAlchemy would otherwise reach for psycopg2, which is not installed, and the `ModuleNotFoundError` reads as a broken install), defaults `sslmode` to `require` and refuses `disable`/`allow`/`prefer`, refuses port 6543, and refuses a bare `postgres` username against the pooler. `test_database_url()` derives from it by renaming the database, so there is one credential and not two that can drift. `db_schema()`, `require_database()`, `cors_origins()` (refuses `*`). |
| `constants.py` | `TREASURY_CAT_ID` (all-zeros sentinel), `TREASURY_HANDLE`, `MAX_AMOUNT` (1e12 per movement), `JS_SAFE_INTEGER` (2^53-1), `API_V1_PREFIX`. |
| `db.py` | `Base` plus the constraint naming convention (`ck_`, `uq_`, `fk_`, `ix_`, `pk_`). The names are load bearing: `test_schema.py` matches them out of `IntegrityError` text. Imports nothing environmental, so importing a model never requires an environment. |
| `models.py` | The three tables and every constraint. Deliberately **schema-unqualified**: `search_path` supplies `meowpay` instead, because schema-qualified models compared against the connection's default schema make autogenerate report every table missing, which is permanent `alembic check` drift. |
| `session.py` | `build_engine()` (app) and `build_migration_engine()` (Alembic and schema setup). `isolation_level` pinned to `READ COMMITTED`: under `REPEATABLE READ` an idempotent replay re-reads on the transaction's original snapshot and cannot see the winning duplicate's committed row. `pool_size`/`max_overflow` are parameters so tests can make the cap hard, since a race test whose threads queue on the pool never races and still passes. A `connect` listener (`insert=True`) applies `search_path` and the session timeouts, with `autocommit` toggled on around the `SET`: without that the statement runs in a transaction and the pool's check-in `ROLLBACK` reverts it, so the handler would work once per connection and then silently stop. It then asserts `current_schema()` landed, because `public` is off the path and a missing schema would otherwise be a successful query against the wrong rows. |

## HTTP layer (`backend/src/meowpay/api/`)

| Module | Responsibility |
|---|---|
| `app.py` | `create_app()` builds the FastAPI app and mounts middleware and routers. `serve()` is the `meowpay-api` console script: reads `API_HOST`/`API_PORT`, runs uvicorn by import string with `factory=True` so `--reload` has something to re-import. |
| `deps.py` | `sessions()` and the `Sessions` annotated alias. Routes reach the session factory through `Depends` and never by import, which is what lets a test redirect them onto a throwaway database. |
| `middleware.py` | Effective chain `RequestID -> CORS -> UnhandledError -> route`. `RequestIDMiddleware` accepts an inbound `X-Request-ID` only if it parses as a UUID, else replaces it, and echoes it on the response. `UnhandledErrorMiddleware` renders the 500 envelope. It is middleware and not an exception handler because Starlette hoists a bare `Exception` handler to `ServerErrorMiddleware`, outside CORS, and the browser would then block every 500 so the frontend could not read the `code` it branches on. |
| `schemas.py` | `HealthResponse`, `HealthStatus`, and `ErrorResponse`, the error envelope. |
| `routes/health.py` | `GET /health`. Reads the treasury row rather than `SELECT 1`, so a reachable but unmigrated database reports unhealthy. 200 when healthy, 503 otherwise. Version from package metadata, falling back to `unknown`. |

### The error envelope

Every 500 renders this shape. The frontend branches on `code`, never on the HTTP status.

| Key | What it is |
|---|---|
| `error.code` | Stable machine-readable string. `internal_error` for anything unhandled |
| `error.message` | Deliberately opaque. Detail goes to the log, keyed by the same request id |
| `error.request_id` | The UUID also returned in the `X-Request-ID` header, so a user-reported failure maps to one log line |

---

## The tables

All three live in the `meowpay` schema, not `public`. See
[decisions.md](decisions.md#where-the-tables-live).

| Table | Holds |
|---|---|
| `cats` | Identity, the `auth_user_id` it signs in as, a materialized `balance`, `is_system` |
| `transfers` | One row per settled movement, transfer or deposit, plus the idempotency key |
| `entries` | The append-only double-entry ledger, two lines per movement summing to zero |

### `cats`

| Column | What it is |
|---|---|
| `id` | UUID PK, generated in Python. The all-zeros sentinel is the treasury |
| `handle` | Unique, lowercase, `^[a-z0-9_]{3,32}$` |
| `auth_user_id` | Unique, nullable. The Supabase `auth.users` id this cat signs in as. **NULL only for the treasury.** No foreign key to `auth.users`, deliberately |
| `balance` | `BIGINT` minor units. Materialized, not `SUM(entries)` |
| `is_system` | True only for the treasury, enforced by CHECK |

### `transfers`

| Column | What it is |
|---|---|
| `kind` | `transfer` or `deposit`. `VARCHAR(16)` plus a CHECK, not a Postgres enum, so extending it needs no `ALTER TYPE` |
| `from_cat_id` / `to_cat_id` | Both FK to `cats` with `ON DELETE RESTRICT` |
| `amount` | `BIGINT`, always positive. Direction lives in `kind` and in the entry signs |
| `idempotency_key` | 8 to 255 characters. Client supplied |
| `owner_cat_id` | **Generated, stored.** `to_cat_id` for a deposit, else `from_cat_id`. The cat that chose the key. Computed by the database so it cannot drift from `kind` and no caller can set it |

### `entries`

Two rows per movement. Three columns name a cat and they are easy to confuse.

| Column | What it is |
|---|---|
| `cat_id` | Whose ledger line this is |
| `counterparty_cat_id` | The other party. Denormalized from `transfers` so a statement is one index scan with no join. Safe to denormalize because entries are append-only and nothing ever rewrites a line |
| `amount` | **Signed.** Negative debits, positive credits. Direction is the sign, so no separate column can disagree with it |
| `balance_after` | The balance this line settled at. Immutable, which is what lets a replayed transfer reproduce its original response without storing response bodies |
| `id` | `BIGSERIAL`. Internal, never an external identifier, and monotonic so it orders a statement for free |

Index `ix_entries_cat_id_id` on `(cat_id, id DESC)` is the only read path: one
cat's statement, newest first.

---

## Constraints

These hold even when the service is wrong.

| Constraint | Stops |
|---|---|
| `ck_cats_balance_non_negative` | An overdraft. Keyed on the treasury's **id**, not on `is_system`, so flipping that flag cannot switch the check off |
| `ck_cats_only_the_sentinel_is_system` | An ordinary cat becoming a system account and so escaping the check above |
| `ck_cats_only_system_lacks_auth_user` | The treasury carrying an identity, and an ordinary cat losing one. Makes an unlinked ordinary cat impossible, so there is never an `UPDATE cats SET auth_user_id` path |
| `uq_cats_auth_user_id` | Two cats sharing one login, and so either spending the other's treats |
| `ck_cats_balance_is_js_safe` | A balance a browser cannot represent exactly. The per-movement cap alone does not bound this: enough deposits still walk past 2^53-1, and the treasury gets there first |
| `uq_transfers_owner_cat_id_idempotency_key` | A duplicate settlement. Also the index the replay lookup reads |
| `uq_entries_transfer_id_cat_id` | One cat credited twice for one movement. Does **not** make the ledger balance: a line for a cat that is not party to the movement is still accepted |
| `ck_transfers_parties_differ` | A self-transfer existing at all |
| `ck_transfers_amount_positive`, `ck_transfers_amount_within_cap` | Non-positive and unrepresentable amounts |

**Not enforced here:** the ledger summing to zero, and deposits originating at
the treasury. Nothing stops a hand-written `INSERT` adding a lone unbalanced
line. Both rest on one module being the only writer, asserted by a
reconciliation test rather than by the schema.

---

## Configuration (environment variables)

Read from `backend/.env`. See `backend/.env.example`.

**Required**

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Supabase **session pooler** URI, port 5432. Normalised and validated on read, see `config.py` above. There is no fallback: a default would let a misconfigured deployment connect to nothing and report it as a database outage |

**Optional**

| Variable | Default | Purpose |
|---|---|---|
| `DB_SCHEMA` | `meowpay` | Where the application's tables live |
| `TEST_DATABASE_URL` | `DATABASE_URL` renamed to `meowpay_test` | The throwaway database the suite creates and drops |
| `MEOWPAY_REQUIRE_DB` | unset | Set to `1` in CI so an unreachable database is an error rather than a skip |
| `CORS_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | Browser origins allowed to call the API. `*` is refused |
| `API_HOST` / `API_PORT` | `127.0.0.1` / `8000` | Read only by `serve()` |

---

## Tests

| File | Covers |
|---|---|
| `conftest.py` | Throwaway `meowpay_test` database created, migrated with Alembic and dropped per session. Per-test isolation is an outer transaction that is never committed, with `join_transaction_mode="create_savepoint"` so code under test can really call `commit()` while the outer transaction still owns the rollback. `_test_db_name()` refuses any database not ending `_test`, because the drops use `WITH (FORCE)` and would succeed in destroying the application's data |
| `test_health.py` | `/health` healthy, unreachable and reachable-but-unmigrated. Plus the 500 envelope carrying CORS headers and leaking nothing, and a forged request id being replaced |
| `test_schema.py` | Every constraint above, driven by raw SQL so the database refuses them even when the application is wrong |

### The skip rule

Every test depends on the `database` fixture and a skip in a session-scoped
fixture is cached and replayed, so an over-broad `except` turns a completely
broken setup into a green run. The rule: **skip only when nothing answered.**

| Failure | Behaviour |
|---|---|
| DNS failure, TCP refused, TLS failure, connect timeout, paused project | Skip, unless `MEOWPAY_REQUIRE_DB=1` |
| Wrong password, unknown project ref, missing privilege, pool exhausted | Error |
| Bad `DATABASE_URL` shape (port 6543, bare username) | Error, raised by `config.py` before any connection |

Discriminating those is less obvious than it looks. A query error carries a
SQLSTATE, but a connection error does not: psycopg leaves `sqlstate` as `None`
for the startup handshake, so `FATAL: password authentication failed` arrives
looking exactly like a DNS failure. The connection case is discriminated on the
Postgres severity marker instead, which is present only when a server replied.
