"""
RLS isolation test suite.

This is the test the brief explicitly asks for: "ship at least one
test that would fail if [brand isolation] broke." It signs in as
each of the 6 real users (via the anon key + their real password —
exactly the path your frontend uses, NOT the service role key, which
bypasses RLS and would make this test meaningless) and verifies:

  1. Each user can see their own brand's data.
  2. Each user gets ZERO rows when the app/attacker tries to read
     another brand's data — even via a raw filter-less select, which
     is what proves RLS is enforcing this, not app-layer filtering.
  3. Analysts cannot write (insert) campaigns; owners can.
  4. Anonymous (no session) requests get nothing from protected tables.

Usage:
    pip install supabase pytest python-dotenv
    Fill in the 6 credentials below (or load from a .env file).
    pytest test_isolation.py -v
"""

import os
import pytest
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]  # anon key, NOT service role

# Fill in the real passwords you set when creating these 6 users.
USERS = {
    "kilele_owner":       ("owner@kilelerides.test",        os.environ.get("PW_KILELE_OWNER", "")),
    "kilele_analyst":     ("analyst@kilelerides.test",      os.environ.get("PW_KILELE_ANALYST", "")),
    "karoo_owner":        ("owner@karoocoaches.test",       os.environ.get("PW_KAROO_OWNER", "")),
    "karoo_analyst":      ("analyst@karoocoaches.test",     os.environ.get("PW_KAROO_ANALYST", "")),
    "marrakech_owner":    ("owner@marrakechexpress.test",   os.environ.get("PW_MARRAKECH_OWNER", "")),
    "marrakech_analyst":  ("analyst@marrakechexpress.test", os.environ.get("PW_MARRAKECH_ANALYST", "")),
}

BRAND_OF = {
    "kilele_owner": "kilele-rides", "kilele_analyst": "kilele-rides",
    "karoo_owner": "karoo-coaches", "karoo_analyst": "karoo-coaches",
    "marrakech_owner": "marrakech-express", "marrakech_analyst": "marrakech-express",
}


def client_for(user_key):
    """Fresh client, signed in as this user via the anon key — the real auth path."""
    email, password = USERS[user_key]
    sb = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    sb.auth.sign_in_with_password({"email": email, "password": password})
    return sb


@pytest.fixture(scope="module")
def anon_client():
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


@pytest.fixture(scope="module")
def brand_ids(anon_client):
    # brand ids are public-ish (needed to even ask "give me brand X's data");
    # fetch via a signed-in client since brands table is also RLS-scoped
    sb = client_for("kilele_owner")
    all_brands = {}
    for slug in ("kilele-rides", "karoo-coaches", "marrakech-express"):
        # each user can only see their OWN brand row, so we can't list all
        # three from one session — resolve each from its own owner instead
        pass
    return {
        "kilele-rides": _own_brand_id("kilele_owner"),
        "karoo-coaches": _own_brand_id("karoo_owner"),
        "marrakech-express": _own_brand_id("marrakech_owner"),
    }


def _own_brand_id(user_key):
    sb = client_for(user_key)
    res = sb.table("brands").select("id").execute()
    assert len(res.data) == 1, f"{user_key} should see exactly their own brand row"
    return res.data[0]["id"]


# ----------------------------------------------------------------
# 1. Every user can see their OWN brand's contacts and campaigns
# ----------------------------------------------------------------

@pytest.mark.parametrize("user_key", USERS.keys())
def test_user_sees_own_brand_data(user_key):
    sb = client_for(user_key)
    contacts = sb.table("contacts").select("id").execute().data
    campaigns = sb.table("campaigns").select("id").execute().data
    assert len(contacts) > 0, f"{user_key} should see their own brand's contacts"
    assert len(campaigns) > 0, f"{user_key} should see their own brand's campaigns"


# ----------------------------------------------------------------
# 2. THE core isolation test — filter-less select must return ONLY
#    the caller's own brand, proving RLS enforces it server-side,
#    not the app adding a WHERE clause. This is the test that fails
#    if someone later removes or weakens the RLS policy.
# ----------------------------------------------------------------

@pytest.mark.parametrize("user_key", USERS.keys())
def test_no_cross_brand_leakage(user_key, brand_ids):
    sb = client_for(user_key)
    own_brand = BRAND_OF[user_key]

    contacts = sb.table("contacts").select("id,brand_id").execute().data
    campaigns = sb.table("campaigns").select("id,brand_id").execute().data
    sends = sb.table("campaign_sends").select("id,brand_id").execute().data

    own_brand_id = brand_ids[own_brand]
    for row in contacts + campaigns + sends:
        assert row["brand_id"] == own_brand_id, (
            f"ISOLATION BREACH: {user_key} received a row from brand_id={row['brand_id']}, "
            f"but should only ever see {own_brand_id}"
        )


@pytest.mark.parametrize("user_key,other_brand", [
    ("kilele_owner", "karoo-coaches"),
    ("kilele_owner", "marrakech-express"),
    ("karoo_owner", "kilele-rides"),
    ("karoo_owner", "marrakech-express"),
    ("marrakech_owner", "kilele-rides"),
    ("marrakech_owner", "karoo-coaches"),
])
def test_explicit_cross_brand_query_returns_nothing(user_key, other_brand, brand_ids):
    """Simulates an attacker/bug explicitly asking for another brand's
    data by id — RLS must return zero rows, not an error and not data."""
    sb = client_for(user_key)
    other_brand_id = brand_ids[other_brand]
    res = sb.table("contacts").select("id").eq("brand_id", other_brand_id).execute()
    assert res.data == [], (
        f"ISOLATION BREACH: {user_key} queried brand_id={other_brand_id} ({other_brand}) "
        f"directly and got {len(res.data)} rows back"
    )


# ----------------------------------------------------------------
# 3. Role enforcement: analysts can read but not write sends
# ----------------------------------------------------------------

@pytest.mark.parametrize("user_key", ["kilele_analyst", "karoo_analyst", "marrakech_analyst"])
def test_analyst_cannot_create_campaign(user_key):
    sb = client_for(user_key)
    with pytest.raises(Exception):
        sb.table("campaigns").insert({
            "brand_id": _own_brand_id(user_key),
            "name": "should not be allowed",
        }).execute()


def test_owner_can_create_and_it_stays_in_their_brand():
    sb = client_for("kilele_owner")
    own_id = _own_brand_id("kilele_owner")
    res = sb.table("campaigns").insert({
        "brand_id": own_id,
        "name": "isolation-test-campaign",
    }).execute()
    assert len(res.data) == 1
    assert res.data[0]["brand_id"] == own_id
    # No DELETE policy is defined for campaigns (schema.sql only grants
    # owners INSERT), so cleanup uses the service role key deliberately,
    # not the tested session — this test's job is to prove the insert
    # succeeded under real RLS, not to double as a delete-permission test.
    admin = create_client(SUPABASE_URL, os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    admin.table("campaigns").delete().eq("id", res.data[0]["id"]).execute()


# ----------------------------------------------------------------
# 4. Anonymous / unauthenticated requests get nothing
# ----------------------------------------------------------------

def test_anonymous_request_sees_no_data(anon_client):
    res = anon_client.table("contacts").select("id").execute()
    assert res.data == [], "Anonymous, unauthenticated requests must never see contact data"
