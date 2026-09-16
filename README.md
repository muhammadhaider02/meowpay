<div align="center">

# MeowPay

**MEOW. MEOW. MEOW.**

[![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://python.org)
[![uv](https://img.shields.io/badge/uv-Package_Manager-DE5FE9?logo=uv&logoColor=white)](https://docs.astral.sh/uv/)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Supabase](https://img.shields.io/badge/Database-Supabase-3FCF8E?logo=supabase&logoColor=white)](https://supabase.com)

A digital wallet for cats. Humans top it up, cats send each other treats.

[API](docs/api.md) · [Architecture](docs/architecture.md) · [Decisions](docs/decisions.md)

</div>

---

One vertical slice of a money-movement product: a cat signs in, sees a balance
and sends treats to another cat. A FastAPI service over Postgres with an
append-only double-entry ledger, row-level locking and idempotent writes, so a
transfer settles exactly once or not at all.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and a free
[Supabase](https://supabase.com) project. That is the whole list.

**1. Create a Supabase project.** The free tier is enough.

**2. Point the app at it.**

```bash
cp backend/.env.example backend/.env
```

Fill in `DATABASE_URL` from the Supabase dashboard under **Connect**, **Direct**,
**Session pooler**, as a URI. Two things matter and both are covered in
`.env.example`: take the **session** pooler on port **5432** rather than the
transaction pooler on 6543, and copy the hostname rather than assembling it,
since newer projects sit behind `aws-1-<region>` and older ones behind `aws-0-`.

**3. Run it.**

```bash
make dev        # install dependencies, then migrate
make seed       # three demo cats, with logins, funded through the ledger
make serve      # api on http://localhost:8000
```

`make seed` needs `SUPABASE_URL` and `SUPABASE_SECRET_KEY` as well, because it
creates the Supabase auth users the cats sign in as. It prints the email and
password for each. Lotus starts with nothing on purpose, so a rejected transfer
can be demonstrated without editing data first. All three are funded through the
ledger rather than by writing balances, so a freshly seeded database reconciles.

`curl localhost:8000/health` reports whether the API can actually reach the
database and whether that database has been migrated. Interactive docs at
<http://localhost:8000/docs>.

### The endpoints

Everything but `/health` needs `Authorization: Bearer <supabase access token>`.
The full contract, including the idempotency rules and every error code, is in
[docs/api.md](docs/api.md).

| | |
|---|---|
| `GET /health` | Reachable, and migrated |
| `POST /api/v1/cats` | Claim a handle for the signed-in account |
| `GET /api/v1/cats` | Who you can send to |
| `GET /api/v1/me` | The signed-in cat, with its balance |
| `GET /api/v1/me/entries` | Statement, newest first |
| `POST /api/v1/transfers` | Send treats |
| `POST /api/v1/deposits` | Top up from the treasury |

Both `POST`s require an `Idempotency-Key` header. Generate it once per intent and
reuse it on every retry of that intent: that is what makes a retry safe, and
regenerating it on retry is how a double spend happens.

The migrations create a `meowpay` schema and put the three tables in it, rather
than using `public`. That is a security decision and not tidiness:
[why](docs/decisions.md#where-the-tables-live).

### Without make

Each target is a one-line wrapper, so `make` is a convenience and never a
dependency:

| Target | Raw command |
|---|---|
| `install` | `cd backend && uv sync --group dev` |
| `migrate` | `cd backend && uv run alembic upgrade head` |
| `seed` | `cd backend && uv run meowpay-seed` |
| `revision` | `cd backend && uv run alembic revision --autogenerate -m "..."` |
| `dev` | `install` then `migrate` |
| `serve` | `cd backend && uv run meowpay-api --reload` |
| `lint` | `cd backend && uv run ruff check src/ tests/` |
| `format` | `cd backend && uv run ruff format src/ tests/` |
| `typecheck` | `cd backend && uv run mypy src/ tests/` |
| `check` | `cd backend && uv run alembic check` |
| `test` | `cd backend && uv run pytest` |
| `test-fast` | `cd backend && uv run pytest -m "not concurrency"` |
| `all` | lint, typecheck and test |

There is no `clean`. To roll the schema back:
`cd backend && uv run alembic downgrade base`.

## Development

`make all` runs lint, typecheck and tests. `make check` runs `alembic check`
separately, because it needs a reachable database and would turn the deliberate
skip below into a hard failure.

Run this once per clone, before the first commit:

```bash
cd backend && uv run pre-commit install
```

Without it the hygiene hooks are configuration and nothing else. They lint,
format and catch trailing whitespace, missing final newlines, unparseable YAML,
files over 500kb, committed private keys and merge conflict markers, and they
run on commit rather than in CI so the history is clean rather than reported on
afterwards. Ruff is pinned to one exact version in both
`.pre-commit-config.yaml` and `backend/pyproject.toml`, because a formatter that
disagrees with itself rewrites the files the other half just approved.

Note that `alembic check` does **not** compare CHECK constraint bodies, so a
Python validator that drifts from its constraint passes it cleanly. The tests
that read `pg_get_constraintdef` are what cover that:
[the guard](docs/architecture.md#the-guard-that-alembic-check-does-not-provide).

Tests needing a database create their own throwaway `meowpay_test` database on
the same Supabase project, migrate it with Alembic and drop it afterwards. The
suite refuses to run against any database whose name does not end in `_test`.

Without a reachable database they skip rather than fail. That convenience is a
hazard, so the skip is narrow: it fires only when nothing answered. A wrong
password or an exhausted pool is an error, because a configuration problem
reported as "not reachable" would hide behind a green run.
[The rule](docs/architecture.md#the-skip-rule).

**Set `MEOWPAY_REQUIRE_DB=1` in CI**, where a skipped suite and a passing suite
are indistinguishable, and where a free-tier project paused after 7 days of
inactivity would otherwise look like success.
