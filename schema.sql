-- ============================================================
-- Velocity Growth Campaign Portal — schema.sql
-- Target: Supabase Postgres (RLS policies require auth.uid() /
-- auth.users, which only exist in a real Supabase project).
-- ============================================================

-- ---------- extensions ----------
create extension if not exists "pgcrypto"; -- gen_random_uuid()

-- ---------- enums ----------
create type user_role as enum ('owner', 'analyst');
create type send_status as enum ('pending', 'sending', 'completed', 'failed');
create type recipient_status as enum ('queued', 'delivered', 'bounced', 'opened', 'unsubscribed');
create type provider_event_type as enum ('delivered', 'bounced', 'opened', 'unsubscribed');

-- ============================================================
-- BRANDS
-- ============================================================
create table brands (
  id          uuid primary key default gen_random_uuid(),
  slug        text unique not null,          -- 'kilele-rides' | 'karoo-coaches' | 'marrakech-express'
  name        text not null,
  created_at  timestamptz not null default now()
);

-- ============================================================
-- PROFILES  (1:1 with auth.users; adds brand + role)
-- ============================================================
create table profiles (
  id          uuid primary key references auth.users(id) on delete cascade,
  brand_id    uuid not null references brands(id) on delete restrict,
  role        user_role not null,
  full_name   text,
  created_at  timestamptz not null default now()
);

create index profiles_brand_idx on profiles(brand_id);

-- helper: current caller's brand_id / role, used everywhere in RLS
create or replace function auth_brand_id() returns uuid
language sql stable security definer set search_path = public as $$
  select brand_id from profiles where id = auth.uid();
$$;

create or replace function auth_role() returns user_role
language sql stable security definer set search_path = public as $$
  select role from profiles where id = auth.uid();
$$;

-- ============================================================
-- CONTACTS
-- ============================================================
create table contacts (
  id              uuid primary key default gen_random_uuid(),
  brand_id        uuid not null references brands(id) on delete cascade,
  external_id     text not null,             -- id from the seed export, for idempotent re-import
  email           text,
  phone           text,
  full_name       text,
  unsubscribed_at timestamptz,               -- null = subscribed
  created_at      timestamptz not null default now(),
  unique (brand_id, external_id)             -- re-loading the same export upserts, doesn't duplicate
);

create index contacts_brand_idx on contacts(brand_id);

-- ============================================================
-- CAMPAIGNS
-- ============================================================
create table campaigns (
  id           uuid primary key default gen_random_uuid(),
  brand_id     uuid not null references brands(id) on delete cascade,
  name         text not null,
  description  text,
  created_by   uuid references profiles(id),
  created_at   timestamptz not null default now()
);

create index campaigns_brand_idx on campaigns(brand_id);

-- ============================================================
-- CAMPAIGN_SENDS  (one attempt to actually send a campaign)
-- ============================================================
create table campaign_sends (
  id               uuid primary key default gen_random_uuid(),
  campaign_id      uuid not null references campaigns(id) on delete cascade,
  brand_id         uuid not null references brands(id) on delete cascade,
  status           send_status not null default 'pending',
  recipient_count  integer not null,          -- count shown + approved on the confirm screen
  batch_id         text,                      -- returned by POST /v1/messages
  idempotency_key  uuid not null default gen_random_uuid(), -- sent as Idempotency-Key header
  requested_by     uuid references profiles(id),
  requested_at     timestamptz not null default now(),
  confirmed_at     timestamptz,
  next_event_cursor text                      -- last `next_cursor` from GET /v1/messages/{id}/events
);

create index campaign_sends_brand_idx on campaign_sends(brand_id);

-- Only one non-terminal send per campaign at a time — this is what
-- makes double-confirm / two-tabs / retry safe at the DB level.
create unique index one_active_send_per_campaign
  on campaign_sends(campaign_id)
  where status in ('pending', 'sending');

-- ============================================================
-- SEND_RECIPIENTS  (snapshot of who a specific send went to)
-- ============================================================
create table send_recipients (
  id                uuid primary key default gen_random_uuid(),
  campaign_send_id  uuid not null references campaign_sends(id) on delete cascade,
  brand_id          uuid not null references brands(id) on delete cascade,
  contact_id        uuid not null references contacts(id),
  provider_ref      text,                    -- id the provider uses for this recipient, if any
  status            recipient_status not null default 'queued',
  updated_at        timestamptz not null default now(),
  unique (campaign_send_id, contact_id)
);

create index send_recipients_send_idx on send_recipients(campaign_send_id);
create index send_recipients_brand_idx on send_recipients(brand_id);

-- ============================================================
-- PROVIDER_EVENTS  (append-only, deduped, source of truth for status)
-- ============================================================
create table provider_events (
  id                uuid primary key default gen_random_uuid(),
  campaign_send_id  uuid not null references campaign_sends(id) on delete cascade,
  brand_id          uuid not null references brands(id) on delete cascade,
  provider_event_id text not null,           -- id from the provider payload, for dedup
  recipient_ref     text not null,           -- matches send_recipients.provider_ref / contact external id
  event_type        provider_event_type not null,
  occurred_at       timestamptz not null,    -- provider's timestamp, NOT our insert time
  raw_payload       jsonb not null,
  received_at       timestamptz not null default now(),
  unique (campaign_send_id, provider_event_id)   -- makes ingestion idempotent
);

create index provider_events_send_idx on provider_events(campaign_send_id);

-- ============================================================
-- LOAD_ERRORS  (surfaces "what didn't load and why")
-- ============================================================
create table load_errors (
  id           uuid primary key default gen_random_uuid(),
  brand_id     uuid not null references brands(id) on delete cascade,
  source_file  text not null,
  row_data     jsonb,
  error_message text not null,
  created_at   timestamptz not null default now()
);

create index load_errors_brand_idx on load_errors(brand_id);

-- ============================================================
-- SHARED_LINKS  (password-protected public results page)
-- ============================================================
create table shared_links (
  id             uuid primary key default gen_random_uuid(),
  campaign_id    uuid not null references campaigns(id) on delete cascade,
  brand_id       uuid not null references brands(id) on delete cascade,
  token          text not null unique default encode(gen_random_bytes(16), 'hex'),
  password_hash  text not null,             -- store a bcrypt/argon2 hash, never plaintext
  created_by     uuid references profiles(id),
  created_at     timestamptz not null default now(),
  expires_at     timestamptz
);

create index shared_links_campaign_idx on shared_links(campaign_id);

-- ============================================================
-- ROW LEVEL SECURITY
-- ============================================================
alter table brands            enable row level security;
alter table profiles          enable row level security;
alter table contacts          enable row level security;
alter table campaigns         enable row level security;
alter table campaign_sends    enable row level security;
alter table send_recipients   enable row level security;
alter table provider_events   enable row level security;
alter table load_errors       enable row level security;
alter table shared_links      enable row level security;

-- brands: any authenticated user can read only their own brand row
create policy brands_select on brands for select
  using (id = auth_brand_id());

-- profiles: a user can see profiles within their own brand
create policy profiles_select on profiles for select
  using (brand_id = auth_brand_id());

-- contacts: read-only for both roles, scoped to brand
create policy contacts_select on contacts for select
  using (brand_id = auth_brand_id());

-- campaigns: read for both roles, scoped to brand
create policy campaigns_select on campaigns for select
  using (brand_id = auth_brand_id());
create policy campaigns_insert on campaigns for insert
  with check (brand_id = auth_brand_id() and auth_role() = 'owner');

-- campaign_sends: read both roles; only owners can create/update
create policy sends_select on campaign_sends for select
  using (brand_id = auth_brand_id());
create policy sends_insert on campaign_sends for insert
  with check (brand_id = auth_brand_id() and auth_role() = 'owner');
create policy sends_update on campaign_sends for update
  using (brand_id = auth_brand_id() and auth_role() = 'owner');

-- send_recipients: read both roles, scoped to brand; writes via
-- service role / edge function only (no direct client insert policy)
create policy send_recipients_select on send_recipients for select
  using (brand_id = auth_brand_id());

-- provider_events: read both roles, scoped to brand; ingestion is
-- done by a trusted server-side job using the service role key,
-- which bypasses RLS — so no insert policy for regular users.
create policy provider_events_select on provider_events for select
  using (brand_id = auth_brand_id());

-- load_errors: read both roles, scoped to brand
create policy load_errors_select on load_errors for select
  using (brand_id = auth_brand_id());

-- shared_links: only owners can see/manage the link rows themselves
-- (the public results page is served through a separate SECURITY
-- DEFINER function, not a direct table read — see below).
create policy shared_links_select on shared_links for select
  using (brand_id = auth_brand_id() and auth_role() = 'owner');
create policy shared_links_insert on shared_links for insert
  with check (brand_id = auth_brand_id() and auth_role() = 'owner');

-- ============================================================
-- PUBLIC SHARED-RESULTS ACCESS
-- Anonymous visitors never query tables directly. They call this
-- function with a token + password; it checks the hash itself and
-- returns only that one campaign's aggregated results.
-- ============================================================
create or replace function get_shared_campaign_results(p_token text, p_password text)
returns jsonb
language plpgsql security definer set search_path = public as $$
declare
  v_link shared_links%rowtype;
  v_result jsonb;
begin
  select * into v_link from shared_links where token = p_token;

  if not found then
    raise exception 'not_found';
  end if;

  if v_link.expires_at is not null and v_link.expires_at < now() then
    raise exception 'expired';
  end if;

  if v_link.password_hash <> crypt(p_password, v_link.password_hash) then
    raise exception 'invalid_password';
  end if;

  select jsonb_build_object(
    'campaign_name', c.name,
    'recipient_count', cs.recipient_count,
    'delivered', count(*) filter (where sr.status = 'delivered'),
    'bounced', count(*) filter (where sr.status = 'bounced'),
    'opened', count(*) filter (where sr.status = 'opened'),
    'unsubscribed', count(*) filter (where sr.status = 'unsubscribed')
  ) into v_result
  from campaigns c
  join campaign_sends cs on cs.campaign_id = c.id
  left join send_recipients sr on sr.campaign_send_id = cs.id
  where c.id = v_link.campaign_id
  group by c.name, cs.recipient_count;

  return v_result;
end;
$$;

-- pgcrypto needed for crypt()/gen_random_bytes() above
create extension if not exists "pgcrypto";
