# Decisions

What was chosen, what was skipped and why. The map of what exists is in
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
| **The sender comes from the token, never from the body** | A `from_handle` field would let anyone spend anyone's treats, and no constraint in the database would stop it. `extra="forbid"` turns an attempt into a 422 rather than a silent no-op |
| **The recipient is named by handle, not by id** | A uuid in a request body invites a client to store one. The handle is what the sender actually typed, and it is normalised by the same function onboarding uses |
| **A deposit takes no recipient field** | It credits the caller, resolved from the token. A target field would be a mint into someone else's account |
| **The idempotency key is a header, not a body field** | It describes the request rather than the movement, and the header name is the one everybody already implements. Presence is checked in the route so the refusal carries `idempotency_key_invalid` rather than Pydantic's generic `validation_error` |
| **201 fresh, 200 on replay** | The status is the only place the distinction is free. A client that reads neither it nor `replayed` still gets the correct answer, which is the point of idempotency |
| **`balance_after`, not `balance`, on a movement** | On a replay it is the historical value from the ledger line. Calling it `balance` would make a week-old replay look like `GET /api/v1/me` contradicting itself |
| **Only `GET /api/v1/me` reports a balance** | One place that can report a stale one. It is read outside any lock, so it is advisory: the ledger rechecks funds under a row lock and that is the only check that counts |
| **Statements paginate by keyset, not OFFSET** | The index is on `(cat_id, id DESC)`, so `id < :before` is a range scan at any depth. OFFSET would also skip or repeat rows whenever a movement lands between requests, which on a live money feed is routine rather than rare |
| **The recipient picker carries no balances** | A directory that reported them would tell every signed-in cat exactly who is worth stealing from. It also excludes the caller, because offering a self transfer invites the error the ledger refuses |
| **Routes translate no errors** | Every ledger and auth rejection already carries its own `code` and status. A `try/except` in a route would be a second place where the status for insufficient funds is decided |
| **A repeated `Idempotency-Key` header is refused** | FastAPI hands a `str`-typed header only the first value and drops the rest, so a client that sent two keys would settle under one and retry under the other. That is a double spend assembled from a header nobody looked at, so the parameter is a list and more than one value is a 422 |
| **Ruff pinned to one exact version, written in two files** | `.pre-commit-config.yaml` and `backend/pyproject.toml` both name it, so a floating `>=` lets the hook and the CLI format the same file two different ways and each rewrites what the other approved. The dev pin is `==`, and the two change together or not at all |
| **The ruff hooks run over `src/` and `tests/` only** | Exactly the paths `make lint` and `make format` pass. Widening the pattern reaches `migrations/`, which pyproject exempts as alembic's generated output, so the hook would restyle generated code the Makefile then leaves alone. The same divergence as an unpinned version, by path rather than by version |
| **Sign-in is by email, never by handle** | GoTrue authenticates an email and has never heard of a handle. `meowpay-seed` creates each demo cat as `{handle}@meowpay.test`, so a form asking for a handle would be asking for the one thing that cannot sign anyone in |
| **The idempotency key lives in `sessionStorage`, not in a React ref** | A key tied to a mounted component is minted again by anything that unmounts one, and switching between the send and top-up tabs does. The sequence that costs a cat real treats is: send, response lost, check the other tab, come back, resend. The backend sees a key it has never seen and settles a second time, which is the exact double spend the header exists to prevent. Keyed per kind of movement, so a send and a top up never collide |
| **The key rotates on a settlement and on `idempotency_key_reused`, and nowhere else** | A settlement, replay included, means the intent is finished. A 409 means the backend is saying this key already names a different movement and the remedy is a new one, which without rotating here is a remedy the UI cannot reach: every later send would fail identically for the life of the tab. Every other failure keeps the key, because a rejected transfer does not consume it and a request that timed out may already have settled |
| **Retry once, and only on `token_expired`** | Safe only because the key is stable across the retry: if the first attempt settled, the second replays it. `ledger_busy` is retryable too but is left to a button, because retrying it silently means retrying something that may already have moved treats |
| **The browser reads no balance from a movement response** | `balance_after` is the balance when the movement settled, and on a replay that is historical. The page refetches `GET /api/v1/me`, so a repeated request cannot make the balance appear to go backwards |
| **No Next.js rewrite proxying `/api` to the backend** | A same-origin rewrite would make CORS untested in development and hide a misconfigured `CORS_ORIGINS` until someone opened the deployed app. The browser talks to FastAPI directly, so the origin list is exercised on every request |
| **The build refuses to run without the three `NEXT_PUBLIC_` values** | They are compiled into the bundle, so a build missing one produces an app that is permanently wrong and silent about it: `NEXT_PUBLIC_API_BASE_URL` would fall back to localhost, and the browser blocks that as mixed content on an https page before it is even sent. The check lives in `next.config.ts` rather than at module scope in `lib/api.ts` because a throw there is only reached while the pages are prerendered, so a later `force-dynamic` would silence it, and because the tests import that module at top level |
| **The boot-time check validates the connection string and never dials it** | A URL naming the transaction pooler is never going to work and should kill the boot. A database that is unreachable is transient and should leave the service up so `/health` reports it honestly. Connecting at startup would collapse the second into the first and turn a blip into a failed deploy |
| **Render's health check points at `/health`, which touches the database** | A build that cannot read the database never replaces one that can, and a dependency-free probe would wave exactly that through. The trade, and what it costs, is in [deployment.md](deployment.md#why-the-health-check-path-is-health) |

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

Deliberately not built. Each is a decision rather than an oversight.

| Not built | Why |
|---|---|
| Storing passwords at all | `cats` carries an `auth_user_id` and no hash, so Supabase Auth owns credentials and this service never sees one. It also means the backend can verify tokens with a public key rather than holding the key they are signed with, and anyone who can read a deployment environment holding a shared secret can mint a token for any cat |
| A deferred constraint trigger for zero-sum | Would make the zero-sum property structural rather than upheld by a single writer. More machinery than this slice earns, and a reconciliation test catches the same class of bug |
| A status column on `transfers` | A movement is one transaction, so a failure rolls the row away and `settled` is the only value it could ever hold |
| A Postgres enum for `kind` | `VARCHAR(16)` plus a `CHECK` instead. Extending a real enum needs `ALTER TYPE ADD VALUE`, which cannot run in the same transaction that adds it and which autogenerate handles badly |
| A generated API client | Seven endpoints and one consumer. A generator is more machinery than the surface earns, and `docs/api.md` is the contract both sides copy from. The cost is stated in the trade-offs below |
| A component library | Four screens and one form pattern. Plain CSS with custom properties covers light and dark in fewer lines than the configuration a library would need |

---

## Trade-offs accepted

| Trade-off | Consequence |
|---|---|
| **The front end tests the request client and the key store, not the components** | 42 tests cover `lib/api.ts` and `lib/idempotency.ts`, which between them are the only parts of the browser that can cause a double spend. The forms and the rendering carry none, so a broken layout is caught by opening the page rather than by a suite. That is the cheap failure to find; the expensive one is covered. |
| **The frontend's types are a hand-written mirror of `schemas.py`** | Nothing fails to compile if a field is renamed on the Python side; it arrives `undefined` at runtime instead. Accepted because the surface is seven endpoints and one consumer, and `docs/api.md` is the shared contract. A generated client is the fix if the surface grows |
| **A signed-out token stays valid until it expires** | Nothing is introspected per request, so signing out revokes the refresh token and not the outstanding access token. Mitigated by a 900 second token lifetime rather than the 3600 default. `session_id` is in the claims, so a denylist is the available seam if it ever needs to be tighter. This is the one security cost of verifying tokens rather than introspecting them, and it buys not having the auth server in the path of every transfer |
| **Nothing in the test suite touches the real auth server** | The verifier is tested against a locally generated key pair, which is what makes those tests fast, offline and exhaustive. The cost is that no test proves Supabase's real tokens carry the claims we require. Verified by hand instead: sign in, verify, resolve to a cat |
| **The treasury is a global write hotspot** | Every deposit locks one row, so deposits serialize. Correct at this scale. At real scale you shard it per region |
| **A failed transfer does not consume its idempotency key** | The opposite of Stripe, and deliberate. A rejection is not a settlement, so retrying after fixing the cause should succeed rather than replay a failure. A caller who wants the Stripe behaviour can use a fresh key |
| **The zero-sum property is not a database constraint** | It holds because `ledger.py` is the only writer, and a reconciliation test asserts it. Saying the schema guarantees it would be false |
| **`UNIQUE (transfer_id, cat_id)` means one leg per cat** | Fine for two-party movements. Fee or FX legs would need revisiting |
| **Free tier sleeps** | The database pauses after 7 days of inactivity. The bring-your-own-project path in the README is the real mitigation |
| **Tests share a project with the application** | The free tier allows two projects in total. Isolation comes from a separate throwaway database and from refusing any database whose name does not end `_test` |
