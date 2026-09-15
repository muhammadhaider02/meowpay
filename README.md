<div align="center">

# MeowPay

**MEOW. MEOW. MEOW.**

[![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://python.org)
[![uv](https://img.shields.io/badge/uv-Package_Manager-DE5FE9?logo=uv&logoColor=white)](https://docs.astral.sh/uv/)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Supabase](https://img.shields.io/badge/Database-Supabase-3FCF8E?logo=supabase&logoColor=white)](https://supabase.com)

A digital wallet for cats. Humans top it up, cats send each other treats.

[Architecture](docs/architecture.md) · [Decisions](docs/decisions.md)

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
make serve      # api on http://localhost:8000
```

`curl localhost:8000/health` reports whether the API can actually reach the
database and whether that database has been migrated. Interactive docs at
<http://localhost:8000/docs>.

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
| `revision` | `cd backend && uv run alembic revision --autogenerate -m "..."` |
| `dev` | `install` then `migrate` |
| `serve` | `cd backend && uv run meowpay-api --reload` |
| `lint` | `cd backend && uv run ruff check src/ tests/` |
| `format` | `cd backend && uv run ruff format src/ tests/` |
| `typecheck` | `cd backend && uv run mypy src/ tests/` |
| `test` | `cd backend && uv run pytest` |
| `test-fast` | `cd backend && uv run pytest -m "not concurrency"` |
| `all` | lint, typecheck and test |

There is no `clean`. To roll the schema back:
`cd backend && uv run alembic downgrade base`.

## Development

`make all` runs lint, typecheck and tests.

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
