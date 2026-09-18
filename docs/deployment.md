# Deployment

The API runs on Render and the web app on Vercel, both wired to this repo
through their dashboards.

There is deliberately no `render.yaml` and no `vercel.json`. Neither platform
reads one when the service is created by hand and committed configuration that
nothing reads is a second source of truth that goes stale silently. The settings
live here instead.

---

## Before the first deploy

Four things, three of which no health check can catch and one of which stops the
deploy going live at all.

### 1. Asymmetric JWT signing keys

The API accepts ES256 and RS256 against the project's key set and has no
shared-secret path, so a backend compromise cannot mint a session. On a project
still using the legacy HS256 secret the key set is **empty**, and the symptom is
not an error you can find by looking: the deploy is green, the page loads,
signing in works and every authenticated request answers 401.

```bash
curl "$SUPABASE_URL/auth/v1/.well-known/jwks.json"   # must list keys
```

Set it under **Authentication, JWT Keys**. Also set the access token expiry to
**900 seconds**: there is no per-request introspection, so a signed-out user's
token stays valid until it expires, and the default hour is too wide a window.

### 2. Email confirmation off

Under **Authentication, Sign In / Providers**. With it on, signing up returns a
user with no session and the form appears to do nothing.

### 3. Migrate the database

```bash
cd backend && uv run alembic upgrade head
```

**This one blocks the deploy.** `/health` reads the treasury row, which
migration `0002` creates, so against an unmigrated database it answers 503,
Render's health check never passes and traffic never moves to the new instance.

Migrations run from a developer machine rather than from Render. The session
pooler is reachable from anywhere, and a release command that migrates on every
deploy would give every future deploy the right to rewrite the schema.

Run it again after any later push that carries a migration. `/health` only
proves the treasury row exists, so a **later** unapplied migration leaves it
green while routes 500. It catches an unmigrated database, not drift.

### 4. Seed it

```bash
make seed
```

Needs `SUPABASE_SECRET_KEY` locally, which is correctly never set on Render. It
prints the demo logins. Without this the app is live and empty.

---

## Render

| Setting | Value |
|---|---|
| Root directory | `backend` |
| Region | Whichever is nearest the Supabase project |
| Build command | `pip install uv && uv sync --frozen --no-dev` |
| Start command | `API_HOST=0.0.0.0 API_PORT=$PORT uv run --no-sync meowpay-api` |
| Health check path | `/health` |
| Environment | `DATABASE_URL`, `SUPABASE_URL`, `CORS_ORIGINS` |

Python needs no setting. `backend/.python-version` pins it and Render reads that.

**`SUPABASE_SECRET_KEY` must not be set here.** It is read by `meowpay-seed` and
by nothing else, and its presence on a public-facing service is a standing
privilege escalation.

Two details in the start command earn their place. `serve()` reads `API_HOST`
and `API_PORT` and **never** `PORT`, so the mapping is mandatory rather than
decorative; note that an empty `PORT` would leave `API_PORT` unset and bind
8000, which Render reports only as "no open ports detected". And `--no-sync`
stops `uv` re-resolving the environment on every boot, which would move a
dependency failure from build time to start time.

---

## Vercel

| Setting | Value |
|---|---|
| Root directory | `frontend` |
| Framework | Next.js, detected. The build command stays `npm run build` |
| Environment | `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`, `NEXT_PUBLIC_API_BASE_URL` |

Scope all three to **Production and Preview**. The build refuses to run without
them, on purpose: they are compiled into the bundle, so a build missing one
produces an app that fails silently in a browser rather than loudly here.

For the same reason, changing one later needs a **redeploy**. Editing the value
alone leaves the old one baked into the deployed bundle.

All three are public by design. The secret key is not among them.

---

## The order, which is circular

Vercel needs the Render URL and Render needs the Vercel one.

1. Deploy **Render** first. Leave `CORS_ORIGINS` at its default for now.
2. Put the Render URL into Vercel's `NEXT_PUBLIC_API_BASE_URL` and deploy.
3. Put the Vercel **production** origin into Render's `CORS_ORIGINS`, with the
   scheme and no trailing slash, and redeploy.

The link Vercel shows immediately after a deploy is a per-deployment host like
`meowpay-a1b2c3-you.vercel.app`, which is **not** the origin in `CORS_ORIGINS`.
Every request from it is blocked by the browser, with nothing in the server log
to explain why, because the request never arrives. Preview deployments are
refused for the same reason and are not supported.

---

## Why the health check path is `/health`

Render will not move traffic to a new instance until the check passes, so a
build that cannot reach or read the database never replaces one that can. That
is automatic protection against a later push breaking a working deploy.

It is a **trade, not a free win**. A health check path governs traffic switching,
not deploy correctness: a database having a bad minute fails a deploy of code
that is fine, and a hotfix cannot ship while it is degraded. A dependency-free
probe would decouple those, at the cost of waving through a build that boots but
cannot see the database, which is the failure that actually matters here.

Watch `connect_timeout`, which is 10 seconds. An unreachable database makes each
probe hang that long, and a platform may report that as a timeout rather than as
a database problem, which sends you looking in the wrong place.

---

## Operating notes

**Render auto-deploys every push to `dev`.** The health check is what stops a
broken push replacing a working deploy.

**Do not run `make test` against the production project while it is in use.**
The suite creates and drops a `meowpay_test` database on the **same** Supabase
project, and the concurrency fixtures build their own engine on top of the
ordinary one, so its peak is the sum of the two rather than either alone. The
live API holds connections at the same time.
