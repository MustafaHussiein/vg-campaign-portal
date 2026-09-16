"""
Campaign portal backend.

Holds the two secrets that must never reach a browser: the Supabase
service-role key and the messaging provider API key.

Trust model
-----------
The browser sends the user's Supabase access token. This server
verifies it against Supabase, then looks the user's brand and role up
server-side from `profiles`. It NEVER trusts a brand_id, role, or
recipient list sent by the client. Writes then go through the
service-role key (which bypasses RLS), so every query here is scoped
explicitly by the verified brand_id — RLS is the second line of
defence, not the only one.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
PROVIDER_BASE_URL = os.environ.get("PROVIDER_BASE_URL", "https://dispatcher-production-72fc.up.railway.app")
PROVIDER_API_KEY = os.environ["PROVIDER_API_KEY"]

admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# TEMPORARY — remove before final submission. Surfaces the real
# exception in the response body so it's visible in the browser's
# Network tab without needing Render's log viewer. Still logs to
# stdout too, so Render logs also show it.
@app.exception_handler(Exception)
async def debug_exception_handler(request: Request, exc: Exception):
    import traceback
    tb = traceback.format_exc()
    print(tb, flush=True)
    return JSONResponse(status_code=500, content={
        "detail": f"{type(exc).__name__}: {exc}",
        "trace_tail": tb[-2000:],
    })


# ----------------------------------------------------------------
# Auth
# ----------------------------------------------------------------

class Caller(BaseModel):
    user_id: str
    brand_id: str
    brand_slug: str
    role: str


async def current_user(authorization: Optional[str] = Header(None)) -> Caller:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    token = authorization.split(" ", 1)[1]

    try:
        user_client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
        user_resp = user_client.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Your session has expired. Sign in again.")

    if not user_resp or not user_resp.user:
        raise HTTPException(status_code=401, detail="Your session has expired. Sign in again.")

    user_id = user_resp.user.id
    prof = (admin.table("profiles").select("brand_id, role, brands(slug)")
            .eq("id", user_id).maybe_single().execute())
    if not prof or not prof.data:
        raise HTTPException(status_code=403, detail="This account isn't set up with a brand.")

    return Caller(
        user_id=user_id,
        brand_id=prof.data["brand_id"],
        brand_slug=prof.data["brands"]["slug"],
        role=prof.data["role"],
    )


def require_owner(caller: Caller):
    if caller.role != "owner":
        raise HTTPException(status_code=403, detail="Only owners can send campaigns.")


# ----------------------------------------------------------------
# Pages
# ----------------------------------------------------------------

def render(name: str):
    def handler(request: Request):
        return templates.TemplateResponse(name, {
            "request": request,
            "supabase_url": SUPABASE_URL,
            "supabase_anon_key": SUPABASE_ANON_KEY,
        })
    return handler


app.get("/", response_class=HTMLResponse)(render("login.html"))
app.get("/dashboard", response_class=HTMLResponse)(render("dashboard.html"))
app.get("/contacts", response_class=HTMLResponse)(render("contacts.html"))
app.get("/campaigns", response_class=HTMLResponse)(render("campaigns.html"))


@app.get("/s/{token}", response_class=HTMLResponse)
def shared_page(request: Request, token: str):
    return templates.TemplateResponse("shared.html", {"request": request, "token": token})


# ----------------------------------------------------------------
# Contacts / campaigns / dashboard
# ----------------------------------------------------------------

@app.get("/api/contacts")
async def list_contacts(caller: Caller = Depends(current_user),
                        page_num: int = 0, page_size: int = 50, search: str = ""):
    page_size = min(max(page_size, 1), 200)
    start = page_num * page_size
    q = (admin.table("contacts")
         .select("id, external_id, full_name, email, country, city, signup_at, "
                 "consent_marketing, unsubscribed_at, suppressed_until", count="exact")
         .eq("brand_id", caller.brand_id)
         .is_("deleted_at", "null"))
    safe = "".join(ch for ch in search if ch.isalnum() or ch in " @.-_").strip()
    if safe:
        q = q.or_(f"full_name.ilike.%{safe}%,email.ilike.%{safe}%,external_id.ilike.%{safe}%")
    res = q.order("signup_at", desc=True).range(start, start + page_size - 1).execute()
    return {"rows": res.data, "total": res.count, "page": page_num, "page_size": page_size}


@app.get("/api/campaigns")
async def list_campaigns(caller: Caller = Depends(current_user)):
    res = (admin.table("campaigns")
           .select("id, external_id, name, channel, sent_at, reported_sent, reported_delivered, "
                   "reported_bounced, reported_opens, reported_clicks, spend")
           .eq("brand_id", caller.brand_id)
           .order("sent_at", desc=True).execute())
    sends = (admin.table("campaign_sends")
             .select("id, campaign_id, status, recipient_count, requested_at, completed_at, batch_id")
             .eq("brand_id", caller.brand_id).execute())
    by_campaign = {}
    for s in sends.data:
        by_campaign.setdefault(s["campaign_id"], []).append(s)
    for row in res.data:
        row["sends"] = by_campaign.get(row["id"], [])
    return {"rows": res.data, "can_send": caller.role == "owner"}


@app.get("/api/dashboard")
async def dashboard(caller: Caller = Depends(current_user)):
    now_iso = datetime.now(timezone.utc).isoformat()

    def count_contacts(apply):
        q = (admin.table("contacts").select("id", count="exact").limit(1)
             .eq("brand_id", caller.brand_id).is_("deleted_at", "null"))
        return apply(q).execute().count

    total = count_contacts(lambda q: q)
    contactable = count_contacts(lambda q: q
                                 .is_("unsubscribed_at", "null")
                                 .eq("consent_marketing", True)
                                 .not_.is_("email", "null")
                                 .or_(f"suppressed_until.is.null,suppressed_until.lte.{now_iso}"))
    unknown = count_contacts(lambda q: q.is_("consent_marketing", "null"))
    unsubscribed = count_contacts(lambda q: q.not_.is_("unsubscribed_at", "null"))

    errors = (admin.table("load_errors").select("error_message")
              .eq("brand_id", caller.brand_id).limit(10000).execute())
    counts = {}
    for e in errors.data:
        counts[e["error_message"]] = counts.get(e["error_message"], 0) + 1

    return {
        "total_customers": total,
        "contactable": contactable,
        "unknown_consent": unknown,
        "unsubscribed": unsubscribed,
        "role": caller.role,
        "load_errors": sorted([{"message": k, "count": v} for k, v in counts.items()],
                              key=lambda x: -x["count"])[:8],
        "load_error_total": len(errors.data),
    }


@app.get("/api/signups")
async def signups(caller: Caller = Depends(current_user)):
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    rows, page_num, size = [], 0, 1000
    while True:
        res = (admin.table("contacts").select("signup_at")
               .eq("brand_id", caller.brand_id).is_("deleted_at", "null")
               .gte("signup_at", since)
               .range(page_num * size, (page_num + 1) * size - 1).execute())
        rows.extend(res.data)
        if len(res.data) < size:
            break
        page_num += 1
    buckets = {}
    for r in rows:
        if r.get("signup_at"):
            day = r["signup_at"][:10]
            buckets[day] = buckets.get(day, 0) + 1
    return {"days": sorted([{"day": k, "count": v} for k, v in buckets.items()],
                           key=lambda x: x["day"])}


@app.get("/api/campaign-performance")
async def campaign_performance(caller: Caller = Depends(current_user)):
    camps = (admin.table("campaigns").select("id, external_id, name, sent_at, reported_sent, "
                                             "reported_delivered, reported_bounced, reported_opens, reported_clicks")
             .eq("brand_id", caller.brand_id).order("sent_at", desc=True).limit(25).execute())
    out = []
    for c in camps.data:
        obs = {}
        for et in ("bounce", "open", "click", "unsubscribe"):
            r = (admin.table("historical_events").select("id", count="exact").limit(1)
                 .eq("campaign_id", c["id"]).eq("brand_id", caller.brand_id)
                 .eq("event_type", et).execute())
            obs[et] = r.count
        out.append({**c, "observed": obs})
    return {"rows": out}


# ----------------------------------------------------------------
# SEND FLOW
# ----------------------------------------------------------------

def fetch_all_contactable(brand_id: str):
    """The single definition of 'contactable', used by both the preview
    and the send itself, so the number approved on the confirmation
    screen and the number of people emailed cannot drift apart."""
    now_iso = datetime.now(timezone.utc).isoformat()
    rows, page_num, size = [], 0, 1000
    while True:
        res = (admin.table("contacts").select("id, external_id, email, full_name")
               .eq("brand_id", brand_id)
               .is_("deleted_at", "null")
               .is_("unsubscribed_at", "null")
               .eq("consent_marketing", True)
               .not_.is_("email", "null")
               .or_(f"suppressed_until.is.null,suppressed_until.lte.{now_iso}")
               .range(page_num * size, (page_num + 1) * size - 1).execute())
        rows.extend(res.data)
        if len(res.data) < size:
            break
        page_num += 1
    return rows


class PreviewRequest(BaseModel):
    campaign_id: str


@app.post("/api/sends/preview")
async def preview_send(body: PreviewRequest, caller: Caller = Depends(current_user)):
    require_owner(caller)
    camp = (admin.table("campaigns").select("id, name")
            .eq("id", body.campaign_id).eq("brand_id", caller.brand_id)
            .maybe_single().execute())
    if not camp or not camp.data:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    existing = (admin.table("campaign_sends").select("id, status, recipient_count")
                .eq("campaign_id", body.campaign_id).eq("brand_id", caller.brand_id)
                .in_("status", ["pending", "sending", "completed"]).execute())

    recipients = fetch_all_contactable(caller.brand_id)
    return {
        "campaign_name": camp.data["name"],
        "recipient_count": len(recipients),
        "sample": [{"name": r.get("full_name"), "email": r["email"]} for r in recipients[:5]],
        "basis": "Opted in to marketing, not unsubscribed, not suppressed after a bounce, "
                 "not deleted, and has an email address.",
        "already_sent": bool(existing.data),
        "existing_send_id": existing.data[0]["id"] if existing.data else None,
    }


class ConfirmRequest(BaseModel):
    campaign_id: str
    expected_count: int = Field(..., ge=0)


@app.post("/api/sends/confirm")
async def confirm_send(body: ConfirmRequest, caller: Caller = Depends(current_user)):
    require_owner(caller)

    camp = (admin.table("campaigns").select("id, name")
            .eq("id", body.campaign_id).eq("brand_id", caller.brand_id)
            .maybe_single().execute())
    if not camp or not camp.data:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    # Already sent (or sending)? Return that one. Combined with the
    # partial unique index on campaign_sends(campaign_id) WHERE status
    # IN ('pending','sending'), this is what turns a double-click, a
    # retry, or two simultaneous sessions into one send, not two.
    existing = (admin.table("campaign_sends")
                .select("id, status, recipient_count, batch_id")
                .eq("campaign_id", body.campaign_id).eq("brand_id", caller.brand_id)
                .in_("status", ["pending", "sending", "completed"]).execute())
    if existing.data:
        s = existing.data[0]
        return {"send_id": s["id"], "status": s["status"], "recipient_count": s["recipient_count"],
                "already_sent": True,
                "message": "This campaign was already sent. Showing the original send."}

    recipients = fetch_all_contactable(caller.brand_id)

    # The count the marketer approved must be the count that goes out.
    # If the audience moved between preview and confirm, refuse rather
    # than quietly emailing a different number of people.
    if len(recipients) != body.expected_count:
        raise HTTPException(status_code=409,
                            detail=f"The audience changed while you were reviewing it — it's now "
                                   f"{len(recipients):,} people, not {body.expected_count:,}. "
                                   f"Check the list and confirm again.")
    if not recipients:
        raise HTTPException(status_code=400, detail="Nobody in this brand is currently contactable.")

    idem_key = str(uuid.uuid4())
    try:
        created = (admin.table("campaign_sends").insert({
            "campaign_id": body.campaign_id,
            "brand_id": caller.brand_id,
            "status": "pending",
            "recipient_count": len(recipients),
            "idempotency_key": idem_key,
            "requested_by": caller.user_id,
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        }).execute())
    except Exception:
        again = (admin.table("campaign_sends").select("id, status, recipient_count")
                 .eq("campaign_id", body.campaign_id).eq("brand_id", caller.brand_id)
                 .in_("status", ["pending", "sending", "completed"]).execute())
        if again.data:
            s = again.data[0]
            return {"send_id": s["id"], "status": s["status"], "recipient_count": s["recipient_count"],
                    "already_sent": True, "message": "Another session already started this send."}
        raise HTTPException(status_code=500, detail="Couldn't start the send. Try again.")

    send_id = created.data[0]["id"]

    # Snapshot exactly who this send goes to, before contacting the
    # provider — so what was approved still reads as approved later,
    # even after the contact list changes.
    snapshot = [{
        "campaign_send_id": send_id,
        "brand_id": caller.brand_id,
        "contact_id": r["id"],
        "provider_ref": r["external_id"],
        "email": r["email"],
        "status": "queued",
    } for r in recipients]
    for i in range(0, len(snapshot), 500):
        admin.table("send_recipients").insert(snapshot[i:i + 500]).execute()

    admin.table("campaign_sends").update({"status": "sending"}).eq("id", send_id).execute()

    payload = {
        "campaign": camp.data["name"],
        "brand": caller.brand_slug,
        "recipients": [{"external_id": r["external_id"], "email": r["email"]} for r in recipients],
    }
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                f"{PROVIDER_BASE_URL}/v1/messages",
                headers={"Authorization": f"Bearer {PROVIDER_API_KEY}",
                         "Idempotency-Key": idem_key,
                         "Content-Type": "application/json"},
                json=payload)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        admin.table("campaign_sends").update({
            "status": "failed", "error_message": str(exc)[:500],
        }).eq("id", send_id).execute()
        raise HTTPException(status_code=502,
                            detail="The messaging provider didn't accept the send. "
                                   "The attempt is recorded and can be retried.")

    admin.table("campaign_sends").update({
        "status": "completed",
        "batch_id": data.get("batch_id"),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", send_id).execute()

    return {"send_id": send_id, "batch_id": data.get("batch_id"),
            "recipient_count": len(recipients),
            "accepted": len(data.get("accepted") or []),
            "rejected": len(data.get("rejected") or []),
            "status": "completed", "already_sent": False}


# ----------------------------------------------------------------
# EVENT RECONCILIATION
# ----------------------------------------------------------------

async def poll_batch_events(send_row: dict) -> int:
    """Pull delivery events for one send and store them idempotently.

    The provider docs claim events arrive exactly once and in order.
    The brief says they will be messy and out of order. This is built
    for the brief: events are stored append-only, deduped on
    (campaign_send_id, provider_event_id), and recipient status is
    then DERIVED from the whole set by precedence — so replaying,
    reordering or duplicating events cannot change the answer.
    """
    batch_id = send_row.get("batch_id")
    if not batch_id:
        return 0

    cursor = send_row.get("next_event_cursor")
    new_events = 0

    async with httpx.AsyncClient(timeout=60) as client:
        for _ in range(200):  # page ceiling, so a bad cursor can't loop forever
            params = {"since": cursor} if cursor else {}
            resp = await client.get(
                f"{PROVIDER_BASE_URL}/v1/messages/{batch_id}/events",
                headers={"Authorization": f"Bearer {PROVIDER_API_KEY}"},
                params=params)
            resp.raise_for_status()
            payload = resp.json()
            events = payload.get("events") or []
            if not events:
                break

            rows = []
            for ev in events:
                ev_id = str(ev.get("event_id") or ev.get("id") or "")
                if not ev_id:
                    continue
                rows.append({
                    "campaign_send_id": send_row["id"],
                    "brand_id": send_row["brand_id"],
                    "provider_event_id": ev_id,
                    "recipient_ref": str(ev.get("external_id") or ev.get("recipient_id")
                                         or ev.get("recipient") or ev.get("email") or ""),
                    "event_type": ev.get("type") or ev.get("event_type"),
                    "occurred_at": ev.get("occurred_at") or ev.get("timestamp")
                                   or datetime.now(timezone.utc).isoformat(),
                    "raw_payload": ev,
                })

            if rows:
                for i in range(0, len(rows), 500):
                    admin.table("provider_events").upsert(
                        rows[i:i + 500],
                        on_conflict="campaign_send_id,provider_event_id").execute()
                new_events += len(rows)

            cursor = payload.get("next_cursor")
            if not payload.get("has_more") or not cursor:
                break

    admin.table("campaign_sends").update({
        "next_event_cursor": cursor,
        "last_polled_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", send_row["id"]).execute()

    admin.rpc("recompute_recipient_status", {"p_send_id": send_row["id"]}).execute()
    return new_events


@app.get("/api/sends/{send_id}")
async def send_status(send_id: str, caller: Caller = Depends(current_user)):
    send = (admin.table("campaign_sends")
            .select("id, campaign_id, status, recipient_count, requested_at, completed_at, "
                    "batch_id, last_polled_at, error_message")
            .eq("id", send_id).eq("brand_id", caller.brand_id).maybe_single().execute())
    if not send or not send.data:
        raise HTTPException(status_code=404, detail="Send not found.")

    breakdown = {}
    for status in ("queued", "delivered", "opened", "bounced", "unsubscribed"):
        r = (admin.table("send_recipients").select("id", count="exact").limit(1)
             .eq("campaign_send_id", send_id).eq("brand_id", caller.brand_id)
             .eq("status", status).execute())
        breakdown[status] = r.count

    ev = (admin.table("provider_events").select("id", count="exact").limit(1)
          .eq("campaign_send_id", send_id).eq("brand_id", caller.brand_id).execute())

    return {"send": send.data, "breakdown": breakdown, "events_received": ev.count}


@app.post("/api/sends/{send_id}/refresh")
async def refresh_send(send_id: str, caller: Caller = Depends(current_user)):
    send = (admin.table("campaign_sends")
            .select("id, brand_id, batch_id, next_event_cursor, status")
            .eq("id", send_id).eq("brand_id", caller.brand_id).maybe_single().execute())
    if not send or not send.data:
        raise HTTPException(status_code=404, detail="Send not found.")
    try:
        count = await poll_batch_events(send.data)
    except Exception:
        raise HTTPException(status_code=502,
                            detail="Couldn't reach the provider for delivery reports. "
                                   "Your existing figures are unchanged.")
    status = await send_status(send_id, caller)
    return {"new_events": count, **status}


# ----------------------------------------------------------------
# SHARED LINKS
# ----------------------------------------------------------------

class ShareRequest(BaseModel):
    campaign_id: str
    password: str = Field(..., min_length=8)


@app.post("/api/share")
async def create_share(body: ShareRequest, request: Request, caller: Caller = Depends(current_user)):
    require_owner(caller)
    camp = (admin.table("campaigns").select("id").eq("id", body.campaign_id)
            .eq("brand_id", caller.brand_id).maybe_single().execute())
    if not camp or not camp.data:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    token = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    admin.rpc("create_shared_link_admin", {
        "p_campaign_id": body.campaign_id,
        "p_brand_id": caller.brand_id,
        "p_password": body.password,
        "p_token": token,
        "p_created_by": caller.user_id,
    }).execute()

    base = str(request.base_url).rstrip("/")
    return {"url": f"{base}/s/{token}", "token": token}


class SharedAccessRequest(BaseModel):
    password: str


@app.post("/api/shared/{token}")
async def read_shared(token: str, body: SharedAccessRequest):
    """Deliberately unauthenticated — this is the stranger's door. All
    checking happens inside the SQL function, which returns one
    campaign's numbers and nothing else, and gives the same error for
    a bad token and a bad password so neither can be probed."""
    try:
        res = admin.rpc("get_shared_campaign_results",
                        {"p_token": token, "p_password": body.password}).execute()
    except Exception:
        raise HTTPException(status_code=401, detail="That link and password don't match.")
    if not res.data:
        raise HTTPException(status_code=401, detail="That link and password don't match.")
    return res.data


@app.get("/healthz")
def healthz():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
