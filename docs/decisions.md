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
| **Asymmetric ES256 tokens, verified against JWKS** | With a shared HS256 secret this service would hold the key GoTrue **mints** with, so anyone who could read the deployment environment could forge a token for any cat. With an asymmetric key it holds only the public half, so a full compromise still cannot mint a session. Rotation also becomes a dashboard action rather than a coordinated secret change |
| **Accepted algorithms are fixed at construction, never read from the token header** | The key set is public by design, so an attacker can fetch the ES256 public key, use its bytes as an HMAC secret and mint an HS256 token. With HS256 in the accepted list alongside a JWKS-resolved key, that token verifies and the attacker is any cat they choose. One algorithm family per process removes it by construction rather than by care |
| **PyJWT, not python-jose** | python-jose is what Supabase's own Python sample uses. Last released in 2021, unmaintained, has had algorithm-confusion CVEs and depends on `ecdsa`, which carries a timing side-channel advisory |
| **No per-request token introspection** | Calling the auth server on every request would put it in the hot path of every transfer. The cost is stated under trade-offs |
| **A key set we could not fetch is 503; a `kid` genuinely absent from one is 401** | Ours versus the caller's. PyJWT's connection error is a **subclass** of its client error, so catching the parent first reads as correct specific-before-broad ordering and turns every network failure into "your credential is bad" |
| **30 seconds of clock leeway, not zero** | Leeway guards `iat`, not only `exp`, and `iat` is stamped on the provider's clock. Zero means one second of skew fails every authentication, reported as `invalid_token`, which tells the client to re-authenticate and mint a token with the same problem. `exp` bounds revocation, so this costs nothing |
| **`cache_keys=False` on the JWKS client** | Enabling it adds a per-`kid` LRU cache with **no** time based expiry, sitting in front of the key set cache that `lifespan` governs. A signing key revoked at the provider would go on being honoured until the process restarted or sixteen other kids evicted it, and nothing on the Supabase side could stop it |
| **The seed resolves cats by identity, never by handle** | The seed handles are not reserved, so anyone can claim `milo` through the onboarding endpoint. Checking the handle first and skipping looks equivalent and deposits 300 real treats into that stranger's account, out of the treasury, with an idempotency key scoped to them. Resolving the auth user first and keying off it removes the whole class |
| **Onboarding is an endpoint, not a trigger on `auth.users`** | A trigger cannot invent a validated handle without trusting client-supplied metadata. A trigger error aborts the sign-up transaction itself, so a taken handle would make sign-up fail with GoTrue's opaque "Database error saving new user" and the identity would never be created. It is invisible to `alembic check`, and it would be a second, elevated, invisible writer of a money table |
| **Repeating onboarding is 200, not an error** | A frontend that fires it on every load of the onboarding page is then safe. The 201 versus 200 split still tells a careful client which happened |
| **Reserved handles are `handle_invalid`, never `handle_taken`** | `handle_taken` would confirm which rows exist. It also stops anyone registering `meowpay_support` and phishing from a name that looks official |
| **`/health` reads the treasury row, not `SELECT 1`** | `SELECT 1` passes against a reachable but unmigrated database, which is the failure a health check most needs to catch after a deploy |
| **One settlement path for transfers and deposits** | A deposit is the same movement with the treasury as sender. One path means one place the zero-sum property can break, and one place to fix it |
| **`Ledger` takes a `sessionmaker`, never a `Session`** | Row locks are released by COMMIT. A caller holding the session could commit mid-flight and drop them, so the ledger owns its transaction boundaries rather than borrowing someone else's |
| **Rejections are typed and raised ahead of the database** | Every CHECK stays a backstop that should never fire. One firing is a bug and a 500, not a user error, which is why none of them are caught |
| **`FOR NO KEY UPDATE`, not `FOR UPDATE`** | `balance` is not a key column, so the weak mode is sufficient and does not conflict with the `FOR KEY SHARE` that foreign key checks take. The strong mode would deadlock against them under load |
| **Lock order comes from `ORDER BY`, not from sorting in Python** | `id IN (...)` compiles to `id = ANY(array)` and array order is not lock order. Sorting the ids in Python looks like it works and does nothing. Asserted against the emitted SQL, because the absence shows up as a deadlock under load and never in a functional test |

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

Passing them in libpq's startup `options` is the obvious approach and it does
not work here. Supavisor parses the startup packet for its own tenant routing
and does not forward arbitrary settings to the backend it owns.

Measured against the real project, connecting through the session pooler with
`-c lock_timeout=3s` in `connect_args`:

```
lock_timeout                          0        (asked for 3s)
statement_timeout                     2min     (asked for 10s; 2min is Supabase's own default)
idle_in_transaction_session_timeout   0        (asked for 15s)
```

The connection succeeds. Nothing errors. The settings are simply absent. `search_path`
and the coarse nets are issued per connection in a `connect` listener instead,
with `autocommit` toggled on around the `SET`, because otherwise the pool's
check-in `ROLLBACK` reverts them and the handler works exactly once per
connection.

**`lock_timeout` goes further and is set per transaction**, as the first
statement inside `_settle`. It is the one setting the money path depends on:
Postgres raises `55P03` when it expires, the ledger translates that to
`LedgerBusyError`, and the caller gets a retryable 503 instead of a hang. Absent,
that path does not exist and a contended transfer waits out the 30s
`statement_timeout` and fails as `57014`, which nothing translates. Scoping it to
the transaction also keeps `idle_in_transaction_session_timeout` off the test
scaffolding, which legitimately holds an open transaction while it waits on a
barrier.

**Eight seconds, derived rather than picked.** A settle is about seven round
trips and the locks are held from the locking SELECT through COMMIT, so at ~50ms
RTT each holder keeps them for roughly 300ms. Waiters queue, so with eight
threads on one pair of rows the last waits about 7 x 300ms = 2.1s before jitter.
Three seconds passes on a good run and fails on a bad one. Eight is still a
defensible production ceiling.

---

## Skipped

Deliberately not built. The exercise is scored against extra surface area, so
each is a decision rather than an oversight. This list grows with the slice.

| Not built | Why |
|---|---|
| Storing passwords at all | `cats` carries an `auth_user_id` and no hash, so Supabase Auth owns credentials and this service never sees one. It also means the backend can verify tokens with a public key rather than holding the key they are signed with, and anyone who can read a deployment environment holding a shared secret can mint a token for any cat |
| A deferred constraint trigger for zero-sum | Would make the zero-sum property structural rather than upheld by a single writer. More machinery than this slice earns, and a reconciliation test catches the same class of bug |
| A status column on `transfers` | A movement is one transaction, so a failure rolls the row away and `settled` is the only value it could ever hold |
| A Postgres enum for `kind` | `VARCHAR(16)` plus a `CHECK` instead. Extending a real enum needs `ALTER TYPE ADD VALUE`, which cannot run in the same transaction that adds it and which autogenerate handles badly |

---

## Trade-offs accepted

| Trade-off | Consequence |
|---|---|
| **A signed-out token stays valid until it expires** | Nothing is introspected per request, so signing out revokes the refresh token and not the outstanding access token. Mitigated by a 900 second token lifetime rather than the 3600 default. `session_id` is in the claims, so a denylist is the available seam if it ever needs to be tighter. This is the one real security regression from minting our own tokens, and it buys not having the auth server in the path of every transfer |
| **Nothing in the test suite touches the real auth server** | The verifier is tested against a locally generated key pair, which is what makes those tests fast, offline and exhaustive. The cost is that no test proves Supabase's real tokens carry the claims we require. Verified by hand instead: sign in, verify, resolve to a cat |
| **The treasury is a global write hotspot** | Every deposit locks one row, so deposits serialize. Correct at this scale. At real scale you shard it per region |
| **A failed transfer does not consume its idempotency key** | The opposite of Stripe, and deliberate. A rejection is not a settlement, so retrying after fixing the cause should succeed rather than replay a failure. A caller who wants the Stripe behaviour can use a fresh key |
| **The zero-sum property is not a database constraint** | It holds because `ledger.py` is the only writer, and a reconciliation test asserts it. Saying the schema guarantees it would be false |
| **`UNIQUE (transfer_id, cat_id)` means one leg per cat** | Fine for two-party movements. Fee or FX legs would need revisiting |
| **Free tier sleeps** | The database pauses after 7 days of inactivity. The bring-your-own-project path in the README is the real mitigation |
| **Tests share a project with the application** | The free tier allows two projects in total. Isolation comes from a separate throwaway database and from refusing any database whose name does not end `_test` |
