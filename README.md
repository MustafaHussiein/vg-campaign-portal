# Velocity Growth — client campaign portal

A multi-tenant campaign portal for three brands (Kilele Rides, Karoo Coaches,
Marrakech Express) on one Supabase database. Six logins, two roles, one set of
tables, and a hard guarantee that no brand ever sees another's data.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Database + auth | Supabase (Postgres) | Fixed by the brief. Postgres RLS is the isolation boundary. |
| Backend | FastAPI (Python) | Holds the two secrets that must never reach a browser: the service-role key and the provider API key. |
| Frontend | Server-rendered Jinja + vanilla JS | No build step, no framework churn. The data guarantees are the product here, not the UI layer. |

Supabase Edge Functions run Deno, not Python, so the send and event-polling
logic lives in the FastAPI service instead. The browser talks to Supabase
directly for auth and for reads that RLS already protects, and to the FastAPI
backend for anything involving a secret.

## Where the guarantees live

**Data isolation** — `schema.sql`, the RLS policy block (`brands_select`
through `shared_links_insert`). Every tenant table has `brand_id` and a policy
of the form `brand_id = auth_brand_id()`, where `auth_brand_id()` resolves the
caller's brand from `profiles` via `auth.uid()`. This is enforced inside
Postgres, so a forgotten `WHERE` clause in application code cannot leak data.

The backend uses the service-role key, which bypasses RLS. That is deliberate —
it needs to write provider events that users must not be able to forge. To stop
that becoming a hole, every query in `main.py` is scoped explicitly by a
`brand_id` the server derived itself from the verified JWT, never one sent by
the client. RLS is the second line of defence, not the only one.

**The isolation test** — `test_isolation.py`. It signs in as all six users with
the anon key (the real auth path) and asserts that a filter-less `select`
returns only the caller's own brand. If a policy is ever dropped or weakened,
`test_no_cross_brand_leakage` fails.

**Double-send safety** — three layers:
1. A partial unique index: `campaign_sends(campaign_id) WHERE status IN ('pending','sending')`.
   Two simultaneous confirms, one row.
2. `confirm_send` returns the existing send rather than creating a second one.
3. An `Idempotency-Key` on the provider call, so a network-level retry is
   delivered once.

**Approved count integrity** — the confirm request carries `expected_count`,
the number shown on screen. If the contactable set moved in between, the server
returns 409 and makes the marketer look again rather than silently emailing a
different number of people. Recipients are snapshotted into `send_recipients`
at confirm time, so what was approved still reads as approved later.

**Out-of-order event handling** — `recompute_recipient_status()` in
`schema_v3.sql`. Provider events are stored append-only and deduped on
`(campaign_send_id, provider_event_id)`. Status is then *derived* from the full
event set by precedence (`unsubscribed > bounced > opened > delivered >
queued`), never last-write-wins. The same events in any order produce the same
answer. Unsubscribes and bounces propagate back to `contacts`, so the
contactable figure stays true to what actually happened.

The provider docs claim the event stream is exactly-once and in order. The
brief says it will be messy and out of order. This is built for the brief.

**The shared link** — `get_shared_campaign_results()` in `schema_v3.sql`.
Anonymous visitors can execute that one function and cannot read any table.
It returns one campaign's numbers and nothing else. Passwords are bcrypt-hashed
via `pgcrypto`. A bad token and a bad password return the identical error after
the same delay, so neither can be probed.

## Counting decisions

Where two people could reasonably count something two ways, the screen says
which way it counted.

**Contactable** = opted in (`consent_marketing IS TRUE`) AND not unsubscribed
AND not suppressed after a bounce AND not soft-deleted AND has an email
address. Unknown consent (`NULL`) counts as **not** contactable — you don't
email someone on a maybe. That is a conservative choice and it is shown on the
dashboard as its own figure so the size of the ambiguity is visible.

**Campaign performance** shows *reported* and *observed* side by side.
`reported_*` is what the provider stated at send time, taken from the seed
export. `observed` is counted from the raw engagement log. They disagree in
this data — Kilele campaign KIL-0016 reports 532 bounces where the log holds
1,853 distinct bounce events. There is no honest way to collapse those into one
number, so both are shown and labelled.

## The seed import

`import_seed.py` handles a deliberately hostile dataset. Each trap and its
handling:

| Issue | Handling |
|---|---|
| Different column names/casing per brand (`email` / `Email` / `e_mail`, `pays`) | Per-brand column map |
| Marrakech uses `;` delimiter and `,` as decimal separator | Per-brand delimiter, `norm_decimal` |
| Invalid UTF-8 bytes | Decoded with `errors="replace"`, logged |
| 3 embedded NUL bytes | Stripped before parsing, logged |
| Header rows repeated as data | `is_header_echo()` detects and skips |
| 2,778 duplicate contact IDs, 2 duplicate campaign IDs | Deduped in memory before upsert, last row wins |
| 77 malformed rows (wrong column count) | Row rejected, logged, import continues |
| `consent_marketing` in 10 formats, `country` in 15 | Normalized via lookup; unrecognized values stored NULL **and logged**, never guessed |
| A phone number in the `country` column | Logged as an anomaly |
| 1,200 dates as `DD/MM/YYYY HH:MM` | Fallback parse, logged |
| 6 CSV formula-injection payloads in `full_name`, including `=cmd\|'/C calc'!A0` | Prefixed with `'` so spreadsheets treat them as literal text |
| Karoo campaign `CMP-014` has `parent_campaign_id = KIL-0007` (another brand) | Rejected and logged; a DB trigger blocks it independently |
| 633 Marrakech events reference 12 campaigns absent from the export | Skipped per campaign, logged with counts |

Re-running the import is safe: every write is an upsert on a natural key, so
loading the same export twice leaves one set of customers.

2,503 issues are recorded in `load_errors` and surfaced on the dashboard, so a
marketer can see what didn't load without emailing anyone.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env     # fill in
```

Run the SQL in order in the Supabase SQL editor:

1. `schema.sql` — tables, RLS policies, helper functions
2. `schema_addendum.sql` — historical campaign/event tables, cross-brand parent trigger
3. `schema_v3.sql` — send pipeline, reconciliation, dashboard RPCs, shared links

Create the six users in Authentication → Users, then link them to brands:

```sql
insert into brands (slug, name) values
  ('kilele-rides','Kilele Rides'),
  ('karoo-coaches','Karoo Coaches'),
  ('marrakech-express','Marrakech Express');
```

then run the `profiles` insert in `seed_profiles.sql`.

Import the data and run the app:

```bash
python import_seed.py /path/to/unzipped/seed
python main.py          # http://localhost:8000
pytest test_isolation.py -v
```

For Google sign-in, set the Supabase Site URL and Redirect URLs, and the Google
Cloud authorized origins, to the deployed URL.

## Known gaps

- Event polling is triggered by a button, not a scheduled worker. Reports that
  arrive while nobody is looking are picked up on the next refresh. A cron
  calling `poll_batch_events` for every send with an open cursor is the
  production shape and is a small change.
- `campaigns` has no `DELETE` policy, so campaigns can't be removed through the
  app. Nothing in the brief asks for it.
- Contact search runs `ILIKE` without a trigram index. Fine at 83k rows,
  would want `pg_trgm` beyond that.
