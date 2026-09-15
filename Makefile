.PHONY: help dev install migrate revision serve lint format typecheck test test-fast all

# Every recipe keeps `cd backend && ...` on a single line on purpose. Make runs each
# recipe line in a fresh shell, and on Windows that shell is cmd.exe rather than
# sh, so a directory change on its own line would not survive to the next one.
#
# Every target below is a thin wrapper. The README prints the raw command beside
# each one, so make stays a convenience and never a dependency for anyone who
# does not have it.
#
# There is no `up`, `down` or `clean`. The database is a hosted Supabase project
# rather than a container, so there is nothing to start and nothing to tear down.
# `clean` in particular is deliberately absent: the old one destroyed a local
# volume, and the honest equivalent here would destroy a real project's data.
# The README documents `alembic downgrade base` as a raw command instead.

help:
	@echo "dev        install dependencies and run migrations"
	@echo "seed       create demo cats and fund them"
	@echo "serve      run the api with hot reload"
	@echo "all        lint, typecheck and test"
	@echo "See the README for the raw command behind each target."

# One command from a clean clone to a migrated schema, once backend/.env exists.
dev: install migrate

install:
	cd backend && uv sync --group dev

migrate:
	cd backend && uv run alembic upgrade head

# usage: make revision m="what it does"
revision:
	cd backend && uv run alembic revision --autogenerate -m "$(m)"

serve:
	cd backend && uv run meowpay-api --reload

lint:
	cd backend && uv run ruff check src/ tests/

format:
	cd backend && uv run ruff format src/ tests/

typecheck:
	cd backend && uv run mypy src/ tests/

test:
	cd backend && uv run pytest

# The inner loop. Every statement is a WAN round trip now, and the concurrency
# tests are where nearly all the wall clock goes, so this keeps `make all` from
# becoming something people stop running.
test-fast:
	cd backend && uv run pytest -m "not concurrency"

all: lint typecheck test
