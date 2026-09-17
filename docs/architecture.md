# MeowPay: the ledger, the schema and the HTTP shell

Navigational map of what exists. Rationale for what was chosen and what was
skipped lives in [decisions.md](decisions.md).

A cat is both the user and the account holder. Humans are not modelled: a human
is a funding source outside the system boundary, so a top-up is a deposit from a
system treasury account rather than a movement between two stored entities.

---

## The settlement path

One transaction, in `ledger.py`. Three of these orderings are load bearing and
each is documented at the line that depends on it.

| Step | What happens |
|---|---|
| 1 | Validate amount and idempotency key in pure Python, before a connection is checked out |
| 1b | Compute `owner_cat_id`, the Python twin of the generated column, and assert it is a party |
| 1c | `set_config` the transaction's own `lock_timeout`, **before anything takes a lock** |
| 2 | Lock both parties `FOR NO KEY UPDATE`, **`ORDER BY id`** |
| 3 | Existence check, from the lock result, **before the claim** |
| 4 | Claim the idempotency key: `INSERT ... ON CONFLICT ON CONSTRAINT ... DO NOTHING RETURNING` |
| 5 | **On conflict, replay and return**, before the funds check |
| 6 | Funds and ceiling checks, on the locked values |
| 7 | Two `UPDATE ... RETURNING`, arithmetic on the SQL side, debit then credit |
| 8 | Both ledger lines in one `INSERT`, so the ledger never holds half a movement |
| 9 | Log **after** COMMIT, outside the transaction |

**Why step 2 comes first**, and it is not the idempotency claim: the overdraft
check and the ceiling check both read a balance that must not move under them,
and consistent acquisition order is what keeps opposing transfers deadlock free.

**Why step 3 precedes step 4**: the claim has two foreign keys to `cats`, so a
missing party would raise a ForeignKeyViolation, abort the transaction, and make
the replay read in step 5 impossible.

**Why step 5 precedes step 6**: retrying a settled transfer must return its
original result even if the sender has since spent everything.

`ORDER BY` is the whole of the lock ordering. Sorting ids in Python looks like it
would do this and does not, because `id IN (...)` compiles to `id = ANY(array)`
and array order is not lock order.

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
│   ├── api.md                       # every endpoint, and the wire contract
│   ├── architecture.md              # this file
│   ├── decisions.md                 # what was chosen, what was skipped, why
│   └── deployment.md                # the render and vercel runbook
│
├── frontend/                        # next.js app router. the only thing a cat sees
│   ├── .env.example                 # three NEXT_PUBLIC_ values, all public by design
│   ├── next.config.ts
│   ├── package.json
│   ├── tsconfig.json
│   ├── vitest.config.mts
│   └── src/
│       ├── app/
│       │   ├── globals.css
│       │   ├── layout.tsx
│       │   ├── page.tsx             # balance, send, top up, statement
│       │   ├── onboarding/page.tsx  # reached on 403 cat_not_onboarded
│       │   └── sign-in/page.tsx     # by email, never by handle
│       ├── components/
│       │   ├── DepositForm.tsx
│       │   ├── Receipt.tsx
│       │   ├── SendForm.tsx
│       │   └── Statement.tsx
│       └── lib/
│           ├── api.test.ts          # the money-path rules
│           ├── api.ts               # token forwarding, envelope, retry once
│           ├── idempotency.test.ts
│           ├── idempotency.ts       # keys that outlive the form that used them
│           ├── session.ts           # the two auth gates
│           ├── supabase.ts          # gotrue only. never reaches the database
│           └── types.ts             # mirrors api/schemas.py
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
    │   ├── auth.py                   # token verification. no HTTP, no database
    │   ├── config.py
    │   ├── constants.py
    │   ├── db.py
    │   ├── errors.py
    │   ├── ledger.py                 # the only writer of balances and entries
    │   ├── models.py
    │   ├── seed.py                   # `make seed`
    │   ├── session.py
    │   └── api/
    │       ├── app.py
    │       ├── deps.py
    │       ├── errors.py             # the exception handlers
    │       ├── middleware.py
    │       ├── schemas.py
    │       └── routes/
    │           ├── __init__.py       # the /api/v1 router everything mounts on
    │           ├── cats.py           # onboarding, and the recipient picker
    │           ├── health.py
    │           ├── me.py             # balance and statement
    │           └── movements.py      # transfers and deposits
    │
    └── tests/
        ├── conftest.py
        ├── test_app.py               # no database, no network
        ├── test_auth_tokens.py       # no database, no network
        ├── test_config.py            # no database, no network
        ├── test_handles.py           # no database, no network
        ├── test_health.py
        ├── test_history_api.py
        ├── test_ledger.py
        ├── test_ledger_concurrency.py
        ├── test_movements_api.py
        ├── test_onboarding.py
        └── test_schema.py
```

---

## Modules (`backend/src/meowpay/`)

| Module | Responsibility |
|---|---|
| `config.py` | Env loading from `backend/.env`. `require_env()` raises at the point of use so a missing variable names itself in the traceback of whatever needed it; `optional_env()` treats empty as unset. `database_url()` does not return what it was given: it rewrites a bare `postgresql://` to `postgresql+psycopg` (SQLAlchemy would otherwise reach for psycopg2, which is not installed, and the `ModuleNotFoundError` reads as a broken install), defaults `sslmode` to `require` and refuses `disable`/`allow`/`prefer`, refuses port 6543, and refuses a bare `postgres` username against the pooler. `test_database_url()` derives from it by renaming the database, so there is one credential and not two that can drift. `database_summary()` renders host, port and database with no credential, because it is the one value here built to be read by a human and put in a log. `db_schema()`, `require_database()`, `cors_origins()` (refuses `*`), and the Supabase identity settings: `supabase_url()`, `jwks_url()`, `jwt_issuer()`, `jwt_audience()`, `supabase_secret_key()`. |
| `api/app.py` | `create_app()` and `serve()`. The `lifespan` resolves `DATABASE_URL`, `SUPABASE_URL` and `CORS_ORIGINS` once at startup and logs a redacted summary, so a deployment missing a value fails with a named error in the first log line rather than one request at a time. It deliberately **opens no connection**: validating the URL string is what separates a URL that is never going to work, which should kill the boot, from a database that is merely unreachable, which should leave the service up so `/health` can say so |
| `ledger.py` | **The only writer of `cats.balance`, `transfers` and `entries`.** `transfer()` and `deposit()` share one `_settle()`, because a deposit is the same movement with the treasury as sender, which is what keeps every entry summing to zero. Returns a frozen `Settlement`, never an ORM object, so a read after the session closes cannot lazy-load. Takes a `sessionmaker` and never a `Session`: row locks are released by COMMIT, so a caller who committed mid-flight would drop them. `_apply_transaction_timeouts()` sets `lock_timeout` per transaction rather than per connection, see [decisions.md](decisions.md#session-settings-do-not-survive-the-pooler). |
| `auth.py` | `TokenVerifier` turns a bearer token into `Claims`, or raises. Imports no FastAPI and takes its key source as a constructor argument, so its suite needs no network. ES256 via JWKS rather than a shared HS256 secret: with a shared secret this service would hold the key GoTrue **mints** with, so anyone who could read the deployment environment could forge a token for any cat. Accepted algorithms come from the verifier's construction and **never** from the token header. `CurrentCat` deliberately carries no balance. |
| `seed.py` | `make seed`. Creates three auth users through the GoTrue admin API with `email_confirm`, links cats to them, then funds through `Ledger.deposit` and never by writing `balance`. Each step is independently idempotent, so a crash part way through is repaired by re-running. Reaches GoTrue over HTTP and never queries `auth.users`, which would hard-code the assumption that the application database is the auth database. |
| `errors.py` | Every rejection as a typed class carrying a stable `code` and an HTTP `status`. Raised ahead of the database, so every CHECK stays a backstop that should never fire: one firing is a bug and a 500, not a user error. |
| `constants.py` | `TREASURY_CAT_ID` (all-zeros sentinel), `TREASURY_HANDLE`, `MAX_AMOUNT` (1e12 per movement), `JS_SAFE_INTEGER` (2^53-1), `API_V1_PREFIX`, and `HANDLE_REGEX`, which `models.py` builds its CHECK from and onboarding validates against, so the two mirrors cannot drift. |
| `db.py` | `Base` plus the constraint naming convention (`ck_`, `uq_`, `fk_`, `ix_`, `pk_`). The names are load bearing: `test_schema.py` matches them out of `IntegrityError` text. Imports nothing environmental, so importing a model never requires an environment. |
| `models.py` | The three tables and every constraint. Deliberately **schema-unqualified**: `search_path` supplies `meowpay` instead, because schema-qualified models compared against the connection's default schema make autogenerate report every table missing, which is permanent `alembic check` drift. |
| `session.py` | `build_engine()` (app) and `build_migration_engine()` (Alembic and schema setup). `isolation_level` pinned to `READ COMMITTED`: under `REPEATABLE READ` an idempotent replay re-reads on the transaction's original snapshot and cannot see the winning duplicate's committed row. `pool_size`/`max_overflow` are parameters so tests can make the cap hard, since a race test whose threads queue on the pool never races and still passes. A `connect` listener (`insert=True`) applies `search_path` and the session timeouts, with `autocommit` toggled on around the `SET`: without that the statement runs in a transaction and the pool's check-in `ROLLBACK` reverts it, so the handler would work once per connection and then silently stop. It then asserts `current_schema()` landed, because `public` is off the path and a missing schema would otherwise be a successful query against the wrong rows. |

## HTTP layer (`backend/src/meowpay/api/`)

| Module | Responsibility |
|---|---|
| `app.py` | `create_app()` builds the FastAPI app and mounts middleware and routers. `serve()` is the `meowpay-api` console script: reads `API_HOST`/`API_PORT`, runs uvicorn by import string with `factory=True` so `--reload` has something to re-import. |
| `deps.py` | `sessions`, `ledger` and `verifier`, plus `claims` and `get_current_cat`. Routes reach all of them through `Depends` and never by import, which is what lets a test redirect them. `claims` and `get_current_cat` are deliberately separate: onboarding needs a verified caller and by definition has no cat row yet, so it can never depend on one. `HTTPBearer(auto_error=False)` because the default raises FastAPI's own 403 with a `{"detail": ...}` body the frontend cannot branch on. Both are plain `def` and never `async def`, because the key fetch is blocking and would otherwise stall the event loop. |
| `middleware.py` | Effective chain `RequestID -> CORS -> UnhandledError -> route`. `RequestIDMiddleware` accepts an inbound `X-Request-ID` only if it parses as a UUID, else replaces it, and echoes it on the response. `UnhandledErrorMiddleware` renders the 500 envelope. It is middleware and not an exception handler because Starlette hoists a bare `Exception` handler to `ServerErrorMiddleware`, outside CORS, and the browser would then block every 500 so the frontend could not read the `code` it branches on. |
| `schemas.py` | Every wire shape. Requests are `strict` and `extra="forbid"`, so a body carrying a field we do not read is a loud 422 rather than a silent no-op. Amounts are deliberately **not** bounded here: `ledger._check_amount` mirrors the CHECK constraints and a second bound would be a second thing to forget. |
| `errors.py` | The exception handlers, and the one place the envelope is built. Registered for the `AppError` base, so Starlette's `__mro__` walk covers every subclass. Also rewrites FastAPI's own `RequestValidationError` and 404/405, which otherwise return `{"detail": ...}` and would give the frontend two error shapes to parse. |
| `routes/cats.py` | `GET /api/v1/cats`, the recipient picker: other cats by handle, no balances, treasury and caller excluded. And `POST /api/v1/cats`, onboarding. Depends on a verified token and **not** on `get_current_cat`, because the caller has no cat row yet. Claim-then-read with `ON CONFLICT DO NOTHING`, which waits on a conflicting in-flight insert rather than skipping it, so two simultaneous sign-ups on one handle give one 201 and one 409. |
| `routes/movements.py` | `POST /api/v1/transfers` and `POST /api/v1/deposits`. Thin on purpose: every rule about what may move lives in the ledger, and every rejection it raises already carries its own code and status, so there is no error translation here. The sender is always the token holder and never a request field. 201 fresh, 200 on replay. |
| `routes/me.py` | `GET /api/v1/me`, the only endpoint that reports a balance, read fresh rather than from the dependency. And `GET /api/v1/me/entries`, the statement, keyset paginated on `(cat_id, id DESC)` because OFFSET would skip or repeat rows on a live feed. |
| `routes/health.py` | `GET /health`. Reads the treasury row rather than `SELECT 1`, so a reachable but unmigrated database reports unhealthy. 200 when healthy, 503 otherwise. Version from package metadata, falling back to `unknown`. |

### The error envelope

Every failure renders one shape, including a 404 on a mistyped path and a 500,
and callers branch on `error.code` rather than on the status. The shape and the
full list of codes are in [api.md](api.md#errors), which is the contract both
sides read.

---

## Modules (`frontend/src/`)

| Module | Responsibility |
|---|---|
| `lib/supabase.ts` | The browser auth client, and the only thing that talks to Supabase directly. It talks to GoTrue and nothing else: the tables live in a private `meowpay` schema PostgREST does not expose, so `supabase.from("cats")` fails even with a valid session. Every balance and every movement comes from FastAPI. A missing `NEXT_PUBLIC_` value throws on first use rather than at import, because the client is built lazily, so this alone would let a build succeed and break on the sign-in button. `next.config.ts` is what prevents that, by refusing a production build without all three values |
| `lib/api.ts` | The one place that calls the API. Reads the token with `getSession()` immediately before each request rather than holding it in state, because supabase-js refreshes in the background and a captured token produces intermittent 401s. Parses the error envelope into an `ApiError` carrying `code`, which is what callers branch on. Treats **200 and 201 alike**: 201 settled something, 200 replayed one, and a client that branches on 201 breaks on every retry. Retries **once**, and only on `token_expired`, which is safe only because the idempotency key is stable across the retry. `ledger_busy` is deliberately not retried behind the user's back |
| `lib/session.ts` | The two auth gates, which are different questions. `getClaims()` answers "is anyone signed in" by verifying the JWT locally; `getSession()` is not used for it, because it reads local storage without revalidating. Whether that identity has a cat is a question only the API can answer, and `403 cat_not_onboarded` is the answer that routes to onboarding. `auth_unavailable` deliberately does not sign anyone out: bouncing every signed-in user to the login screen during a Supabase blip makes the blip worse. A refresh that fails **after** a movement settled is non-fatal and leaves the page standing, because replacing it with an error screen would report a successful send as a failure. Every load carries a generation and only the newest may commit, since two really do overlap |
| `lib/idempotency.ts` | Where an idempotency key lives, and deliberately not a React ref. A key tied to a mounted component is minted again by anything that unmounts one, and switching between the send and top-up tabs does exactly that: an attempt whose response was lost, a tab switch, then a resubmit reaches the backend under a key it has never seen and settles a second time. So the key sits in `sessionStorage`, one slot per kind of movement, surviving unmount and reload. It rotates on a settlement and on `idempotency_key_reused`, and on nothing else, because a rejected transfer does not consume its key |
| `components/SendForm.tsx`, `components/DepositForm.tsx` | The money path. Both take their key from `lib/idempotency` rather than owning one, rotate it after a settlement, and rotate it on `idempotency_key_reused` so the backend's own remedy is reachable from the UI instead of wedging the form. Both also guard on a ref rather than relying on the disabled attribute having been flushed |
| `components/Receipt.tsx` | Labels `balance_after` as "balance at the time" rather than "your balance", since on a replay it is the value from the original ledger line and may be weeks old |
| `components/Statement.tsx` | Keyset pagination. `next_before` comes from the API and is passed straight back, never constructed here |
| `app/page.tsx` | Ties them together. When a movement settles it refetches `GET /api/v1/me` rather than painting `movement.balance_after`, so a replay cannot make the balance appear to go backwards |

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

Read from `backend/.env`. **`backend/.env.example` is the canonical list**, with
a note against each variable; the two below are the ones without a usable
default, and `frontend/.env.example` covers the browser bundle separately.

**Required**

Both are resolved by the lifespan at startup, so the application refuses to boot
without them rather than failing one request at a time.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Supabase **session pooler** URI, port 5432. Normalised and validated on read, see `config.py` above. There is no fallback: a default would let a misconfigured deployment connect to nothing and report it as a database outage |
| `SUPABASE_URL` | The project, which is also where the JWKS URL, the token issuer and the GoTrue admin endpoint are derived from unless each is set explicitly |

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

Two suites. `cd backend && uv run pytest` is 203 tests and owns every guarantee
about money. `cd frontend && npm test` is 43 and owns the parts of the browser
that could cause a double spend: the request client and the key store.

| File | Covers |
|---|---|
| `conftest.py` | Throwaway `meowpay_test` database created, migrated with Alembic and dropped per session. Per-test isolation is an outer transaction that is never committed, with `join_transaction_mode="create_savepoint"` so code under test can really call `commit()` while the outer transaction still owns the rollback. `_test_db_name()` refuses any database not ending `_test`, because the drops use `WITH (FORCE)` and would succeed in destroying the application's data |
| `test_health.py` | `/health` healthy, unreachable and reachable-but-unmigrated. Plus the 500 envelope carrying CORS headers and leaking nothing, and a forged request id being replaced |
| `test_app.py` | What is mounted, and whether the OpenAPI schema can be built at all. No database and no network: the startup checks are given well formed fakes that nothing dials. Also that a malformed connection string stops the application starting, and that the startup log names the database without carrying its password |
| `test_config.py` | The refusals a misconfigured deployment walks into, with no database and no network: the transaction pooler port, an `sslmode` that permits plaintext, a bare `postgres` username against the pooler, a missing or empty variable, and `*` in `CORS_ORIGINS`. Also that `database_summary()` carries no password, since that string exists to be logged |
| `test_schema.py` | Every constraint above, driven by raw SQL so the database refuses them even when the application is wrong |
| `test_auth_tokens.py` | The verifier, with a locally generated ES256 key pair. **No database and no network**, so these run anywhere. Algorithm confusion, `alg: none`, wrong issuer, wrong audience, expiry, a non-uuid subject, anonymous users, unknown `kid`, clock skew, and the ours-versus-theirs split between an unreachable key set (503) and a kid that is genuinely absent (401) |
| `test_handles.py` | `normalise_handle` and `normalise_display_name`, called directly. No database, no network. These exist because `OnboardRequest` strips and bounds its input before the route runs, so several guards are **unreachable** from an endpoint test and are covered nowhere else |
| `test_movements_api.py` | Transfers and deposits over HTTP, with the ledger and the database real. The sender coming from the token and not the body, replay as a 200, a ledger refusal arriving in the envelope with CORS headers, and the 403 `CurrentCatDep` raises for a verified token with no cat |
| `test_history_api.py` | Balance, statement and directory. Double entry seen from both sides, the keyset walk paging to the end without repeating a row, and the directory excluding the caller and the treasury |
| `frontend/src/lib/api.test.ts` | The request client, in Node with no browser. What actually goes on the wire, since a route or a body that is never asserted is a route that can be pointed anywhere; a retry reusing its idempotency key **and** carrying a freshly read token, which are jointly the only reason retrying is safe; 201 and 200 both being success, so a replay is not reported as a failure; the retry firing once and only for `token_expired`, never for a `ledger_busy` that may already have moved treats; the abort budget covering the body read and not just the headers |
| `frontend/src/lib/idempotency.test.ts` | That a key outlives the component that used it, survives a reload, stays steady when `sessionStorage` is denied, and changes only when the intent is over. These are the assertions that catch the tab-switch double spend |
| `test_onboarding.py` | `POST /api/v1/cats` over HTTP with only the cryptography stubbed, so the cat lookup, the 403, the handle collisions and the envelope all run for real |
| `test_ledger.py` | Settlement, idempotent replay, every typed rejection, and reconciliation. Two tests assert on the **emitted SQL** rather than behaviour, because `ORDER BY` and `FOR NO KEY UPDATE` are plan-shape properties whose absence shows up as a deadlock under load and never in a functional test |
| `test_ledger_concurrency.py` | Eight threads on real connections with real commits: one key settles exactly once, eight transfers cannot overdraw, opposing transfers do not deadlock, and the ledger still reconciles afterwards. Plus the `lock_timeout` to `55P03` to 503 chain |

### The guard that `alembic check` does not provide

Autogenerate never reflects or compares `CHECK` constraints, so building
`HANDLE_PATTERN` from `HANDLE_REGEX` proves nothing on its own: the live
constraint is a literal in migration `0001`.

`test_the_handle_check_in_the_database_matches_the_one_the_service_enforces` is
what compares them. It asks the live constraint and the Python validator about
the same strings and requires them to agree. Without it, widening
`HANDLE_REGEX` by one character leaves `alembic check` clean and the suite green
while the endpoint returns 500 on a handle the service accepts and the database
refuses.

Two more pairs are guarded the same way, and both are reachable by a caller:

| Python | SQL constraint |
|---|---|
| `normalise_handle` / `HANDLE_REGEX` | `ck_cats_handle_shape` |
| `_check_idempotency_key`, 8 to 255 | `ck_transfers_idempotency_key_shape` |
| `_check_amount` / `MAX_AMOUNT` | `ck_transfers_amount_positive`, `ck_transfers_amount_within_cap` |

Each test reads `pg_get_constraintdef` and then asks both sides about the same
values. Any Python validator added later that mirrors a `CHECK` needs one too;
`make check` will not catch it.

### Why the concurrency tests cannot pass vacuously

`sessions_factory` binds every session to one rolled-back connection, so threads
on it would share a Postgres backend and never actually race. The `race` fixture
uses a separate engine and four devices to make a fake pass impossible:

| Device | What it stops |
|---|---|
| Two barriers, `warm` then `go` | Without them the OS serialises by start time and the slowest thread runs alone |
| Backend pid recorded **while** `warm` holds every session open | Collecting pids afterwards proves nothing: one connection reused N times reports one pid N times |
| `max_overflow=0` on the pool | Otherwise SQLAlchemy opens 10 more past `pool_size` and a test meant to starve the pool quietly succeeds |
| `assert len(set(pids)) == threads` | A pool that serialised the threads fails the test instead of passing. It also catches a `DATABASE_URL` pointing at the transaction pooler, where connections are multiplexed |

If that assertion ever fires, the answer is not to lower `CONCURRENCY_THREADS`.
That silently weakens six tests and leaves them green.

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
