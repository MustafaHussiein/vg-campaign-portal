-- ============================================================
-- Addendum to schema.sql — run this AFTER schema.sql
-- Adds support for historical seed data (pre-existing campaign
-- performance figures + raw historical engagement log), kept
-- separate from live provider_events (which come from sends made
-- through this app during the build).
-- ============================================================

-- "As reported" aggregate figures, taken at face value from the
-- seed export (campaigns.csv). This is one of two legitimate ways
-- to answer "how did this campaign perform" — see historical_events
-- below for the other.
alter table campaigns add column reported_sent       integer;
alter table campaigns add column reported_delivered  integer;
alter table campaigns add column reported_bounced    integer;
alter table campaigns add column reported_opens      integer;
alter table campaigns add column reported_clicks     integer;
alter table campaigns add column spend               numeric(12,2);
alter table campaigns add column sent_at             timestamptz;
alter table campaigns add column channel             text;
alter table campaigns add column parent_campaign_id  uuid references campaigns(id);
-- parent_campaign_id is only ever set after verifying the referenced
-- campaign shares the same brand_id — enforced in the import script,
-- not here, since a same-table FK can't easily cross-check brand_id
-- without a trigger. Add that trigger too, for defense in depth:

create or replace function enforce_parent_campaign_same_brand()
returns trigger language plpgsql as $$
begin
  if new.parent_campaign_id is not null then
    if (select brand_id from campaigns where id = new.parent_campaign_id) <> new.brand_id then
      raise exception 'parent_campaign_id must belong to the same brand';
    end if;
  end if;
  return new;
end;
$$;

create trigger trg_campaign_parent_same_brand
  before insert or update on campaigns
  for each row execute function enforce_parent_campaign_same_brand();

-- campaigns needs a stable external key to upsert against during import
alter table campaigns add column external_id text;
create unique index campaigns_brand_external_uidx on campaigns(brand_id, external_id);

-- contacts needs the fields the seed data actually carries, since
-- "contactable" depends on consent + suppression + deletion, not
-- just unsubscribed_at.
alter table contacts add column consent_marketing boolean;      -- null = unknown/unparseable, NOT the same as false
alter table contacts add column country            text;         -- normalized ISO-2 where recognizable, else NULL
alter table contacts add column city               text;
alter table contacts add column signup_at          timestamptz;
alter table contacts add column deleted_at         timestamptz;  -- soft-deleted: exclude from all counts, don't hard-delete
alter table contacts add column suppressed_until   timestamptz;  -- temporarily not contactable, has a known expiry

-- Historical engagement log, imported from *-events.csv. Distinct
-- from provider_events: this is pre-existing historical data, not
-- live webhook/poll data from a send made through this app.
create table historical_events (
  id                  uuid primary key default gen_random_uuid(),
  brand_id            uuid not null references brands(id) on delete cascade,
  campaign_id         uuid not null references campaigns(id) on delete cascade,
  contact_id          uuid references contacts(id),   -- nullable: some rows may reference contacts not present in the contacts file
  source_event_id     text not null,                  -- original event_id from the CSV, for de-dup
  event_type          text not null,                  -- bounce | click | complaint | open | unsubscribe
  channel             text,
  occurred_at         timestamptz not null,
  created_at          timestamptz not null default now(),
  unique (campaign_id, source_event_id)
);

create index historical_events_campaign_idx on historical_events(campaign_id);
create index historical_events_brand_idx on historical_events(brand_id);

alter table historical_events enable row level security;
create policy historical_events_select on historical_events for select
  using (brand_id = auth_brand_id());

-- Historical send batches, imported from kilele-send-log.csv (only
-- Kilele has one in the seed data — other brands may simply lack
-- pre-existing batch records, which is fine).
create table historical_send_batches (
  id                uuid primary key default gen_random_uuid(),
  brand_id          uuid not null references brands(id) on delete cascade,
  campaign_id       uuid not null references campaigns(id) on delete cascade,
  batch_key         text not null,
  recipient_count   integer not null,
  status            text not null,
  queued_at         timestamptz not null,
  unique (campaign_id, batch_key)
);

alter table historical_send_batches enable row level security;
create policy historical_send_batches_select on historical_send_batches for select
  using (brand_id = auth_brand_id());
