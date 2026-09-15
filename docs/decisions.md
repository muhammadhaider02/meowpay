# Decisions

What was chosen, what was skipped and why. Grown alongside the code, so it
covers what exists. The map of what exists is in
[architecture.md](architecture.md).

---

## Shipped

| Decision | Why |
|---|---|
| **Supabase as the only database** | Nothing to install and nothing to start, so the deployed app and a fresh clone reach the same kind of database. A reviewer creates a free project and pastes one connection string |
| **Session pooler, port 5432** | A long lived backend wants one server connection per client connection. That keeps prepared statements valid, lets session level settings hold, and gives each thread its own Postgres backend. `config.py` refuses 6543 outright, because one digit separates the two and all three consequences are silent |
| **`DATABASE_URL` normalised on read, not trusted** | Four refusals, each a likely copy-paste whose symptom otherwise points elsewhere: a bare `postgresql://` resolves to psycopg2 and fails as a broken install, `sslmode=prefer` silently falls back to plaintext, port 6543 breaks three things quietly, and a bare `postgres` username returns Supavisor's undiagnosable `Tenant or user not found` |
| **No fallback for `DATABASE_URL`** | A default would let a misconfigured deployment connect to nothing and report it as a database outage rather than a missing variable |
| **Tables in a `meowpay` schema** | See [below](#where-the-tables-live) |
| **`BIGINT` minor units** | No floats and no decimals anywhere in the stack. Treats are whole things |
| **Materialized `balance`, not `SUM(entries)`** | Not about read speed. A derived balance has nothing to lock, since `FOR UPDATE` locks rows that exist and cannot stop a concurrent `INSERT` of a new entry, so read-sum-then-write would need `SERIALIZABLE` and a retry loop. It also cannot carry a `CHECK`, because `CHECK` is per-row and cannot reference an aggregate over another table. The price is real derived state, paid by making one module the only writer of both |
| **Idempotency key unique per `owner_cat_id`** | Scoping to the sender looks right and is a trap. Every deposit shares the treasury as sender, so that column is constant across deposits and the constraint degenerates to a global unique. Two cats topping up with the same key would collide, and a replay would hand the second cat the first cat's amount and balance |
| **Treasury created by migration, not by a seed** | The ledger's zero-sum property depends on the row existing, migrations run in every environment including a test database, and no test should have to seed demo data first |
| **Identity is `auth_user_id`, with no foreign key to `auth.users`** | A cat outlives its login: deleting an identity must not delete a money account. A foreign key would also force every test fixture to create a real `auth.users` row, dragging the auth server into the schema suite. `auth.users` is Supabase's to change, not ours to depend on |
| **Throwaway test database, not a test schema** | Supabase allows it: `postgres` has `CREATEDB`, the pooler routes to the new database and `DROP ... WITH (FORCE)` works against a live session. Measured, not assumed. A whole database means no routing mistake in `conftest.py` can reach the application's rows |
| **Alembic, not Supabase migrations** | `models.py` stays the single source of truth and `alembic check` catches drift. One migration tool rather than two that can disagree |
| **`/health` reads the treasury row, not `SELECT 1`** | `SELECT 1` passes against a reachable but unmigrated database, which is the failure a health check most needs to catch after a deploy |

## Where the tables live

Supabase runs PostgREST over the `public` schema, and the publishable key that
authorises those requests ships inside the browser bundle. Tables created in
`public` are granted to `anon` by default. So a table in `public` means

```
PATCH /rest/v1/cats?handle=eq.milo  {"balance": 999999999}
```

is a path around every invariant the application enforces, available to anyone
who opens devtools.

Two independent answers, because either alone is undone by one future mistake:

| Layer | Effect |
|---|---|
| Tables in `meowpay`, not `public` | PostgREST serves only the schemas it is configured to expose. Asking for this one by name returns `Invalid schema: meowpay. Only the following schemas are exposed: public, graphql_public` |
| RLS enabled with **zero policies**, plus `REVOKE` from `anon` and `authenticated` | No policy means no access for any role except the table owner. Owners bypass RLS unless `FORCE` is set, so the application is untouched |

The schema is applied through `search_path` rather than `Base.metadata.schema`,
because schema-qualified models compared against the connection's default schema
make autogenerate report every table missing, which is permanent `alembic check`
drift. `public` is left off the path so a missing or mistyped schema fails on the
first statement rather than succeeding against the wrong rows.

This is the one place the hosted database is stronger than a local one would be.
"The application is the only writer" stops being a convention.

## Session settings do not survive the pooler

The timeouts used to ride in libpq startup `options`. Supavisor parses the
startup packet for its own tenant routing and does not forward arbitrary
settings to the backend it owns.

Measured against the real project, connecting through the session pooler with
`-c lock_timeout=3s` in `connect_args`:

```
lock_timeout                          0        (asked for 3s)
statement_timeout                     2min     (asked for 10s; 2min is Supabase's own default)
idle_in_transaction_session_timeout   0        (asked for 15s)
```

The connection succeeds. Nothing errors. The settings are simply absent. They are
issued per connection in a `connect` listener instead, with `autocommit` toggled
on around the `SET`, because otherwise the pool's check-in `ROLLBACK` reverts
them and the handler works exactly once per connection.

---

## Skipped

Deliberately not built. The exercise is scored against extra surface area, so
each is a decision rather than an oversight. This list grows with the slice.

| Not built | Why |
|---|---|
| Storing passwords at all | Supabase Auth owns them, so the backend holds only a public key. With a shared secret, anyone who can read the deployment environment can mint a token for any cat, which for a money service is the whole ballgame |
| A deferred constraint trigger for zero-sum | Would make the zero-sum property structural rather than upheld by a single writer. More machinery than this slice earns, and a reconciliation test catches the same class of bug |
| A status column on `transfers` | A movement is one transaction, so a failure rolls the row away and `settled` is the only value it could ever hold |
| A Postgres enum for `kind` | `VARCHAR(16)` plus a `CHECK` instead. Extending a real enum needs `ALTER TYPE ADD VALUE`, which cannot run in the same transaction that adds it and which autogenerate handles badly |

---

## Trade-offs accepted

| Trade-off | Consequence |
|---|---|
| **The treasury is a global write hotspot** | Every deposit will lock one row, so deposits serialize. Correct at this scale. At real scale you shard it per region |
| **`UNIQUE (transfer_id, cat_id)` means one leg per cat** | Fine for two-party movements. Fee or FX legs would need revisiting |
| **Free tier sleeps** | The database pauses after 7 days of inactivity. The bring-your-own-project path in the README is the real mitigation |
| **Tests share a project with the application** | The free tier allows two projects in total. Isolation comes from a separate throwaway database and from refusing any database whose name does not end `_test` |
