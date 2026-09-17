<div align="center">

# MeowPay

**MEOW. MEOW. MEOW.**

[![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://python.org)
[![uv](https://img.shields.io/badge/uv-Package_Manager-DE5FE9?logo=uv&logoColor=white)](https://docs.astral.sh/uv/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.141-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Next.js](https://img.shields.io/badge/Next.js-15-000000?logo=nextdotjs&logoColor=white)](https://nextjs.org)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.9-3178C6?logo=typescript&logoColor=white)](https://typescriptlang.org)
[![Supabase](https://img.shields.io/badge/Database-Supabase-3FCF8E?logo=supabase&logoColor=white)](https://supabase.com)

A digital wallet for cats. Humans top it up, cats send each other treats.

[API](docs/api.md) · [Architecture](docs/architecture.md) · [Decisions](docs/decisions.md) · [Deployment](docs/deployment.md)

</div>

---

## The slice

One vertical slice of a money-movement product, end to end: a cat signs in, sees
a balance, sends treats to another cat and reads a statement.

A FastAPI service over Postgres with an append-only double-entry ledger, row
level locking and idempotent writes, so a transfer settles exactly once or not
at all. A Next.js front end is the only part a cat sees. Both talk to a hosted
Supabase project, which owns the database and the identities and nothing else.

`meowpay.ledger` is the only writer of `cats.balance`, `transfers` and
`entries`. The tables live in a private schema PostgREST does not expose, with
row level security and no policies, so a browser cannot reach them at all.
[Why](docs/decisions.md#where-the-tables-live).

## Quickstart

Requires [uv](https://docs.astral.sh/uv/), Node 20+ and a free
[Supabase](https://supabase.com) project. That is the whole list.

```bash
cp backend/.env.example backend/.env       # then fill in the three required values
make dev                                   # install, then migrate
make seed                                  # three demo cats, funded through the ledger
make serve                                 # api on http://localhost:8000
```

`DATABASE_URL`, `SUPABASE_URL` and `SUPABASE_SECRET_KEY`. The first two are
resolved at startup, so the API refuses to boot without them rather than failing
one request at a time; the third is read by `make seed` alone.

Take `DATABASE_URL` from the Supabase dashboard under **Connect**, **Session
pooler**, as a URI. Port **5432**, not the transaction pooler on 6543, and copy
the hostname rather than assembling it. `.env.example` explains why for each.

Then the web app, in a second terminal:

```bash
cp frontend/.env.example frontend/.env.local
cd frontend && npm install && npm run dev  # http://localhost:3000
```

`make seed` prints the shared password once and then an email per cat. Lotus
starts with nothing on purpose, so a rejected transfer can be demonstrated
without editing data first.

Verify with `curl localhost:8000/health`, which reports whether the database is
reachable **and** migrated. Interactive docs at `/docs`; the wire contract,
idempotency rules and every error code are in [api.md](docs/api.md).

## Configuration

Environment variables only. **`backend/.env.example` and `frontend/.env.example`
are the canonical lists**, with a note against each explaining what it is for and
what goes wrong without it. Copy them and fill them in.

`DATABASE_URL` and `SUPABASE_URL` are resolved at startup. `SUPABASE_SECRET_KEY`
is read by `make seed` and by nothing else, so it belongs on a developer machine
and never on a deployed service.

## Development

```bash
make lint        # ruff
make format      # ruff, in place
make typecheck   # mypy
make test        # pytest
make all         # lint + typecheck + test
make check       # alembic check, needs a reachable database
```

`make test-fast` skips the concurrency suite, which is where nearly all the wall
clock goes. Every target is a thin wrapper around one `uv run` command, apart
from `dev` and `all` which chain others, so the Makefile is readable as the list
of raw commands if you would rather not use `make`.

The front end is npm and not wrapped: `npm run dev`, `npm test`,
`npm run typecheck` and `npm run build`, from `frontend/`.

Run `cd backend && uv run pre-commit install` once per clone, or the hygiene
hooks never execute.

Tests that need a database create and drop their own throwaway one on the same
Supabase project, and refuse any name not ending `_test`. Without a reachable
database they skip rather than fail, and that skip is deliberately narrow:
[the rule](docs/architecture.md#the-skip-rule). Note that `alembic check` does
**not** compare CHECK constraint bodies, so the tests that read
`pg_get_constraintdef` are what cover that drift:
[the guard](docs/architecture.md#the-guard-that-alembic-check-does-not-provide).

## Deployment

The API runs on Render and the web app on Vercel, both wired to this repo
through their dashboards. There is no committed platform config, because neither
service reads one when it is created by hand.

The runbook, including the three prerequisites no health check can catch, is in
[deployment.md](docs/deployment.md).
