-- ============================================================
-- schema_v3.sql — run AFTER schema.sql and schema_addendum.sql
-- Send pipeline, event reconciliation, dashboard RPCs, shared links.
-- ============================================================

-- ---------- campaign_sends: fuller lifecycle ----------
alter table campaign_sends add column if not exists error_message text;
alter table campaign_sends add column if not exists completed_at timestamptz;
alter table campaign_sends add column if not exists last_polled_at timestamptz;

-- ---------- send_recipients: keep the email we actually sent to ----------
alter table send_recipients add column if not exists email text;

-- ============================================================
-- CONTACTABILITY — one definition, used everywhere
-- Defined once as a SQL function so the confirm screen, the
-- dashboard, and the send itself can never disagree about who
-- is contactable. If two screens show different numbers, it's
-- because they asked different questions, not because two
-- queries drifted apart.
--
-- A contact is contactable when ALL of:
--   - not soft-deleted
--   - marketing consent is explicitly true (NULL consent counts
--     as NOT contactable — we don't email on a maybe)
--   - not unsubscribed
--   - not currently suppressed (suppressed_until in the future)
--   - has an email address
-- ============================================================
create or replace function is_contactable(c contacts) returns boolean
language sql immutable as $$
  select c.deleted_at is null
     and c.consent_marketing is true
     and c.unsubscribed_at is null
     and (c.suppressed_until is null or c.suppressed_until <= now())
     and c.email is not null
     and c.email <> '';
$$;

create index if not exists contacts_contactable_idx
  on contacts(brand_id)
  where deleted_at is null and unsubscribed_at is null and consent_marketing is true;

-- ============================================================
-- EVENT RECONCILIATION
-- Derives each recipient's status from the FULL set of events
-- received, by precedence — never last-write-wins. This is what
-- makes out-of-order and duplicate delivery reports safe: the
-- same set of events produces the same answer regardless of the
-- order they arrived in.
--
-- Precedence (highest wins):
--   unsubscribed > bounced > opened > delivered > queued
-- ============================================================
create or replace function recompute_recipient_status(p_send_id uuid)
returns void language plpgsql security definer set search_path = public as $$
begin
  update send_recipients sr
  set status = sub.derived_status,
      updated_at = now()
  from (
    select sr2.id,
           case
             when bool_or(pe.event_type = 'unsubscribed') then 'unsubscribed'
             when bool_or(pe.event_type = 'bounced')      then 'bounced'
             when bool_or(pe.event_type = 'opened')       then 'opened'
             when bool_or(pe.event_type = 'delivered')    then 'delivered'
             else 'queued'
           end::recipient_status as derived_status
    from send_recipients sr2
    left join provider_events pe
      on pe.campaign_send_id = sr2.campaign_send_id
     and pe.recipient_ref = sr2.provider_ref
    where sr2.campaign_send_id = p_send_id
    group by sr2.id
  ) sub
  where sr.id = sub.id
    and sr.status is distinct from sub.derived_status;

  -- Unsubscribes and hard bounces change who is contactable in future.
  -- This is the "your picture of who's contactable has to stay correct
  -- against what actually happened" requirement.
  update contacts c
  set unsubscribed_at = coalesce(c.unsubscribed_at, now())
  from send_recipients sr
  where sr.campaign_send_id = p_send_id
    and sr.contact_id = c.id
    and sr.status = 'unsubscribed'
    and c.unsubscribed_at is null;

  update contacts c
  set suppressed_until = greatest(coalesce(c.suppressed_until, now()), now() + interval '100 years')
  from send_recipients sr
  where sr.campaign_send_id = p_send_id
    and sr.contact_id = c.id
    and sr.status = 'bounced'
    and (c.suppressed_until is null or c.suppressed_until < now() + interval '99 years');
end;
$$;

-- ============================================================
-- DASHBOARD RPCs
-- Written as SECURITY INVOKER (the default) so RLS still applies —
-- these are convenience aggregations, NOT a way around isolation.
-- ============================================================

-- Signups per day over the last 30 days
create or replace function dashboard_signups_30d()
returns table(day date, signups bigint)
language sql stable as $$
  select date_trunc('day', signup_at)::date as day, count(*)
  from contacts
  where signup_at >= now() - interval '30 days'
    and deleted_at is null
  group by 1
  order by 1;
$$;

-- Headline counts
create or replace function dashboard_totals()
returns table(total_customers bigint, contactable bigint, unknown_consent bigint)
language sql stable as $$
  select
    count(*) filter (where deleted_at is null),
    count(*) filter (where is_contactable(contacts.*)),
    count(*) filter (where deleted_at is null and consent_marketing is null)
  from contacts;
$$;

-- Per-campaign performance. Returns BOTH the figure the provider
-- reported at send time AND the figure observed in the raw event
-- log, because they disagree in the seed data and there is no
-- honest way to collapse them into one number.
create or replace function campaign_performance()
returns table(
  campaign_id uuid,
  external_id text,
  name text,
  channel text,
  sent_at timestamptz,
  reported_sent int,
  reported_delivered int,
  reported_bounced int,
  reported_opens int,
  reported_clicks int,
  observed_bounced bigint,
  observed_opens bigint,
  observed_clicks bigint,
  observed_unsubscribes bigint
)
language sql stable as $$
  select
    c.id, c.external_id, c.name, c.channel, c.sent_at,
    c.reported_sent, c.reported_delivered, c.reported_bounced,
    c.reported_opens, c.reported_clicks,
    count(distinct he.id) filter (where he.event_type = 'bounce'),
    count(distinct he.id) filter (where he.event_type = 'open'),
    count(distinct he.id) filter (where he.event_type = 'click'),
    count(distinct he.id) filter (where he.event_type = 'unsubscribe')
  from campaigns c
  left join historical_events he on he.campaign_id = c.id
  group by c.id
  order by c.sent_at desc nulls last;
$$;

-- Data-quality summary, so a marketer can see what didn't load
-- without emailing anyone.
create or replace function load_error_summary()
returns table(error_message text, occurrences bigint)
language sql stable as $$
  select error_message, count(*)
  from load_errors
  group by error_message
  order by count(*) desc;
$$;

-- ============================================================
-- SHARED LINKS — password hashing
-- ============================================================
create or replace function create_shared_link(p_campaign_id uuid, p_password text)
returns text language plpgsql security definer set search_path = public as $$
declare
  v_brand uuid;
  v_token text;
begin
  -- SECURITY DEFINER means RLS is bypassed inside this function, so
  -- the caller's right to share THIS campaign must be checked here
  -- explicitly rather than relied on from the policy.
  select brand_id into v_brand from campaigns where id = p_campaign_id;
  if v_brand is null then
    raise exception 'campaign not found';
  end if;
  if v_brand <> auth_brand_id() then
    raise exception 'not your campaign';
  end if;
  if auth_role() <> 'owner' then
    raise exception 'only owners can publish results';
  end if;
  if length(coalesce(p_password, '')) < 8 then
    raise exception 'password must be at least 8 characters';
  end if;

  v_token := encode(gen_random_bytes(16), 'hex');

  insert into shared_links (campaign_id, brand_id, token, password_hash, created_by)
  values (p_campaign_id, v_brand, v_token, crypt(p_password, gen_salt('bf', 10)), auth.uid());

  return v_token;
end;
$$;

-- Called by the backend with the service-role key, where auth.uid()
-- is null. The caller's brand and role are verified in the API layer
-- before this runs; the brand check is repeated here so a mistake in
-- the API can't publish another brand's campaign.
create or replace function create_shared_link_admin(
  p_campaign_id uuid, p_brand_id uuid, p_password text,
  p_token text, p_created_by uuid)
returns text language plpgsql security definer set search_path = public as $$
declare v_brand uuid;
begin
  select brand_id into v_brand from campaigns where id = p_campaign_id;
  if v_brand is null or v_brand <> p_brand_id then
    raise exception 'campaign does not belong to this brand';
  end if;
  if length(coalesce(p_password, '')) < 8 then
    raise exception 'password must be at least 8 characters';
  end if;

  insert into shared_links (campaign_id, brand_id, token, password_hash, created_by)
  values (p_campaign_id, p_brand_id, p_token, crypt(p_password, gen_salt('bf', 10)), p_created_by);

  return p_token;
end;
$$;

-- Replaces the earlier version: returns only this one campaign's
-- results, never the brand, the contact list, or anything adjacent.
create or replace function get_shared_campaign_results(p_token text, p_password text)
returns jsonb
language plpgsql security definer set search_path = public as $$
declare
  v_link shared_links%rowtype;
  v_result jsonb;
begin
  select * into v_link from shared_links where token = p_token;

  -- Same error for "no such link" and "wrong password", so the
  -- response can't be used to discover which tokens exist.
  if not found then
    perform pg_sleep(0.3);
    raise exception 'invalid_link_or_password';
  end if;

  if v_link.expires_at is not null and v_link.expires_at < now() then
    raise exception 'expired';
  end if;

  if v_link.password_hash <> crypt(p_password, v_link.password_hash) then
    perform pg_sleep(0.3);
    raise exception 'invalid_link_or_password';
  end if;

  select jsonb_build_object(
    'campaign_name', c.name,
    'channel', c.channel,
    'sent_at', c.sent_at,
    'reported_sent', c.reported_sent,
    'reported_delivered', c.reported_delivered,
    'reported_bounced', c.reported_bounced,
    'reported_opens', c.reported_opens,
    'reported_clicks', c.reported_clicks,
    'observed_bounced', (select count(*) from historical_events he where he.campaign_id = c.id and he.event_type = 'bounce'),
    'observed_opens',   (select count(*) from historical_events he where he.campaign_id = c.id and he.event_type = 'open'),
    'observed_clicks',  (select count(*) from historical_events he where he.campaign_id = c.id and he.event_type = 'click')
  ) into v_result
  from campaigns c
  where c.id = v_link.campaign_id;

  return v_result;
end;
$$;

-- Anonymous visitors must be able to CALL the function but never
-- read the underlying tables.
grant execute on function get_shared_campaign_results(text, text) to anon;
revoke all on shared_links from anon;
