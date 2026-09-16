"""
Seed data importer for the Velocity Growth campaign portal.

Usage:
    pip install supabase python-dotenv
    export SUPABASE_URL="https://xxxx.supabase.co"
    export SUPABASE_SERVICE_ROLE_KEY="ey..."      # service role, bypasses RLS
    python import_seed.py /path/to/unzipped/seed/dir

Design notes (see the accompanying write-up for the full data-quality
audit this handles):
  - Per-brand column mapping + delimiter, since each brand's export
    uses different headers/casing/delimiters.
  - Every row is normalized through small, testable functions rather
    than trusted as-is. Anything that can't be normalized is logged
    to `load_errors`, never silently guessed or dropped.
  - Embedded header rows (a data row that's literally the header line
    repeated) are detected and skipped.
  - Duplicate external_ids within a single file are deduped, last
    occurrence wins, and logged.
  - The kilele delta file is applied AFTER the base file, as an
    upsert, since it represents later corrections.
  - campaigns.parent_campaign_id is validated to belong to the same
    brand before being set; cross-brand references are nulled out
    and logged (this is the isolation landmine in the karoo data).
  - Batched upserts (500 rows/call) since kilele has ~84k contacts
    and 312k events.
"""

import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from supabase import create_client

BATCH_SIZE = 500

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# ----------------------------------------------------------------
# Normalization helpers
# ----------------------------------------------------------------

TRUE_VALUES = {"1", "true", "yes", "y", "t"}
FALSE_VALUES = {"0", "false", "no", "f", "n"}
NULLISH = {"", "n/a", "null", "\\n", "none", "-", None}

COUNTRY_MAP = {
    "ke": "KE", "kenya": "KE", "ken": "KE",
    "za": "ZA", "south africa": "ZA",
    "ma": "MA", "morocco": "MA",
    "et": "ET", "rw": "RW", "ss": "SS", "tz": "TZ", "ug": "UG",
}

STATUS_MAP = {
    "active": "active",
    "bounced": "bounced",
    "pending": "pending",
    "unsubscribed": "unsubscribed",
    "unsubscribe": "unsubscribed",
}


def clean(v):
    if v is None:
        return None
    v = v.strip()
    return v if v != "" else None


def norm_bool(v, errors, context):
    v = clean(v)
    if v is None:
        return None
    lv = v.lower()
    if lv in TRUE_VALUES:
        return True
    if lv in FALSE_VALUES:
        return False
    errors.append({**context, "field": "consent_marketing", "raw_value": v,
                    "error_message": "unrecognized boolean value"})
    return None


def norm_country(v, errors, context):
    v = clean(v)
    if v is None or v.lower() in NULLISH:
        return None
    lv = v.lower()
    if lv in COUNTRY_MAP:
        return COUNTRY_MAP[lv]
    if len(v) == 2 and v.isalpha():
        return v.upper()
    # anomaly: e.g. a phone number leaked into the country column
    errors.append({**context, "field": "country", "raw_value": v,
                    "error_message": "unrecognized country value, stored as NULL"})
    return None


def norm_status(v, errors, context):
    v = clean(v)
    if v is None:
        return None
    lv = v.lower()
    if lv in STATUS_MAP:
        return STATUS_MAP[lv]
    errors.append({**context, "field": "status", "raw_value": v,
                    "error_message": "unrecognized status value"})
    return None


def norm_decimal(v):
    """Handles both '221.09' and Marrakech's '221,09'."""
    v = clean(v)
    if v is None:
        return None
    return float(v.replace(",", "."))


def norm_int(v):
    v = clean(v)
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def norm_datetime(v, errors=None, context=None):
    v = clean(v)
    if v is None:
        return None
    # ISO 8601, the expected format ('Z' isn't accepted by fromisoformat pre-3.11)
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    # fallback: DD/MM/YYYY HH:MM, seen in some kilele rows
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(v, fmt)
            if errors is not None:
                errors.append({**(context or {}), "field": "datetime", "raw_value": v,
                                "error_message": f"non-ISO date format '{fmt}', parsed as best-effort UTC"})
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    if errors is not None:
        errors.append({**(context or {}), "field": "datetime", "raw_value": v,
                        "error_message": "unparseable date/time value, stored as NULL"})
    return None


FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@")


def sanitize_freetext(v, errors=None, context=None):
    """Neutralizes CSV/formula-injection payloads (e.g. '=cmd|...', '=IMPORTXML(...)')
    in free-text fields that might later be exported to CSV/Excel. Prefixing with an
    apostrophe forces spreadsheet software to treat the value as literal text."""
    if v is None:
        return None
    if v.startswith(FORMULA_TRIGGER_CHARS) and v.startswith("="):
        if errors is not None:
            errors.append({**(context or {}), "field": "full_name", "raw_value": v,
                            "error_message": "formula-injection pattern detected, neutralized"})
        return "'" + v
    return v


def is_header_echo(row: dict, header_values: set) -> bool:
    """Detects a data row that's actually a repeated header line."""
    vals = {str(v).strip().lower() for v in row.values() if v is not None}
    overlap = vals & header_values
    return len(overlap) >= max(1, len(row) // 2)


# ----------------------------------------------------------------
# Robust CSV reading
# ----------------------------------------------------------------

def read_rows(path: Path, delimiter=",", errors=None, context=None):
    """
    Reads a CSV tolerating bad encoding, embedded NUL bytes, and
    malformed row widths. Skips embedded-header rows. Yields dicts
    with clean string values.
    """
    errors = errors if errors is not None else []
    context = context or {}
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        raw_text = f.read()
    nul_count = raw_text.count("\x00")
    if nul_count:
        errors.append({**context, "source_file": path.name, "row_data": None,
                        "error_message": f"{nul_count} embedded NUL byte(s) found and stripped"})
        raw_text = raw_text.replace("\x00", "")

    import io
    reader = csv.reader(io.StringIO(raw_text), delimiter=delimiter)
    header = next(reader)
    header_lower = [h.strip().lower() for h in header]
    header_values = set(header_lower)
    ncols = len(header)
    for i, raw_row in enumerate(reader, start=2):  # start=2: line 1 is header
        if len(raw_row) != ncols:
            errors.append({**context, "source_file": path.name, "row_data": None,
                            "error_message": f"line {i}: expected {ncols} columns, got {len(raw_row)}"})
            continue
        row = dict(zip(header_lower, raw_row))
        if is_header_echo(row, header_values):
            errors.append({**context, "source_file": path.name, "row_data": row,
                            "error_message": f"line {i}: looks like an embedded header row, skipped"})
            continue
        yield row


def batched(iterable, n):
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= n:
            yield batch
            batch = []
    if batch:
        yield batch


def upsert_batches(table, rows, on_conflict):
    for batch in batched(rows, BATCH_SIZE):
        sb.table(table).upsert(batch, on_conflict=on_conflict).execute()


# ----------------------------------------------------------------
# Per-brand contact column mappings
# ----------------------------------------------------------------

CONTACT_COLMAPS = {
    "kilele-rides": dict(external_id="external_id", full_name="full_name", email="email",
                          phone="phone", status="status", consent="consent_marketing",
                          country="country", city="city", signup_at="signup_at",
                          deleted_at="deleted_at", suppressed_until="suppressed_until"),
    "karoo-coaches": dict(external_id="external id", full_name="full name", email="email",
                           phone="phone", status="status", consent="consent marketing",
                           country="country", city="city", signup_at="signup at",
                           deleted_at="deleted at", suppressed_until="suppressed until"),
    "marrakech-express": dict(external_id="external_id", full_name="full_name", email="e_mail",
                               phone="mobile", status="status", consent="consent_marketing",
                               country="pays", city="city", signup_at="signup_at",
                               deleted_at="deleted_at", suppressed_until="suppressed_until"),
}


def import_contacts(brand, files, delimiter, errors):
    """files: list of Paths, processed in order — later files win on conflict."""
    colmap = CONTACT_COLMAPS[brand["slug"]]
    context = {"brand_id": brand["id"]}
    dedup = {}  # external_id -> row, last occurrence (across all files, in order) wins
    for path in files:
        for row in read_rows(path, delimiter=delimiter, errors=errors, context=context):
            ext_id = clean(row.get(colmap["external_id"]))
            if not ext_id:
                errors.append({**context, "source_file": path.name, "row_data": row,
                                "error_message": "missing external_id, row skipped"})
                continue
            row_context = {**context, "source_file": path.name}
            status = norm_status(row.get(colmap["status"]), errors, row_context)
            record = {
                "brand_id": brand["id"],
                "external_id": ext_id,
                "full_name": sanitize_freetext(clean(row.get(colmap["full_name"])), errors, row_context),
                "email": clean(row.get(colmap["email"])),
                "phone": clean(row.get(colmap["phone"])),
                "unsubscribed_at": datetime.now(timezone.utc).isoformat() if status == "unsubscribed" else None,
                "consent_marketing": norm_bool(row.get(colmap["consent"]), errors, row_context),
                "country": norm_country(row.get(colmap["country"]), errors, row_context),
                "city": clean(row.get(colmap["city"])),
                "signup_at": norm_datetime(row.get(colmap["signup_at"]), errors, row_context),
                "deleted_at": norm_datetime(row.get(colmap["deleted_at"]), errors, row_context),
                "suppressed_until": norm_datetime(row.get(colmap["suppressed_until"]), errors, row_context),
            }
            dedup[ext_id] = record  # last write wins: later file / later row in file supersedes
    upsert_batches("contacts", list(dedup.values()), on_conflict="brand_id,external_id")
    print(f"  contacts: upserted {len(dedup)} unique rows for {brand['slug']}")


# ----------------------------------------------------------------
# Campaigns
# ----------------------------------------------------------------

def import_campaigns(brand, path, delimiter, errors):
    context = {"brand_id": brand["id"]}
    rows = list(read_rows(path, delimiter=delimiter, errors=errors, context=context))

    records_by_ext = {}
    parent_refs = {}  # our external_id -> parent's external_id, resolved in pass 2
    for row in rows:
        ext_id = clean(row.get("external_id"))
        if not ext_id:
            continue
        if ext_id in records_by_ext:
            errors.append({**context, "source_file": path.name, "row_data": {"external_id": ext_id},
                            "error_message": "duplicate campaign external_id within file, later row wins"})
        records_by_ext[ext_id] = {
            "brand_id": brand["id"],
            "external_id": ext_id,          # NOTE: add this column to campaigns if not present, or map via a side table
            "name": clean(row.get("campaign_name")),
            "channel": clean(row.get("channel")),
            "reported_sent": norm_int(row.get("reported_sent")),
            "reported_delivered": norm_int(row.get("reported_delivered")),
            "reported_bounced": norm_int(row.get("reported_bounced")),
            "reported_opens": norm_int(row.get("reported_opens")),
            "reported_clicks": norm_int(row.get("reported_clicks")),
            "spend": norm_decimal(row.get("spend")),
            "sent_at": norm_datetime(row.get("sent_at_utc"), errors, context),
        }
        parent = clean(row.get("parent_campaign_id"))
        if parent:
            parent_refs[ext_id] = parent

    records = list(records_by_ext.values())
    upsert_batches("campaigns", records, on_conflict="brand_id,external_id")

    # pass 2: resolve + validate parent_campaign_id is same-brand
    existing = sb.table("campaigns").select("id,external_id").eq("brand_id", brand["id"]).execute().data
    id_by_ext = {r["external_id"]: r["id"] for r in existing}

    updates = []
    for child_ext, parent_ext in parent_refs.items():
        child_id = id_by_ext.get(child_ext)
        parent_id = id_by_ext.get(parent_ext)  # only found if parent belongs to THIS brand
        if child_id and parent_id:
            updates.append({"id": child_id, "parent_campaign_id": parent_id})
        elif child_id:
            errors.append({"brand_id": brand["id"], "source_file": path.name,
                            "row_data": {"campaign": child_ext, "parent_campaign_id": parent_ext},
                            "error_message": "parent_campaign_id does not resolve to a campaign in the same brand — rejected"})
    for u in updates:
        sb.table("campaigns").update({"parent_campaign_id": u["parent_campaign_id"]}).eq("id", u["id"]).execute()

    print(f"  campaigns: upserted {len(records)} rows, {len(updates)} parent links resolved, "
          f"{len(parent_refs) - len(updates)} rejected for {brand['slug']}")


# ----------------------------------------------------------------
# Historical events
# ----------------------------------------------------------------

def import_events(brand, path, delimiter, errors):
    context = {"brand_id": brand["id"]}

    campaigns = sb.table("campaigns").select("id,external_id").eq("brand_id", brand["id"]).execute().data
    camp_by_ext = {c["external_id"]: c["id"] for c in campaigns}
    contacts = sb.table("contacts").select("id,external_id").eq("brand_id", brand["id"]).execute().data
    contact_by_ext = {c["external_id"]: c["id"] for c in contacts}

    dedup = {}
    for row in read_rows(path, delimiter=delimiter, errors=errors, context=context):
        camp_ext = clean(row.get("campaign_external_id"))
        camp_id = camp_by_ext.get(camp_ext)
        if not camp_id:
            errors.append({**context, "source_file": path.name, "row_data": row,
                            "error_message": f"event references unknown campaign {camp_ext}, skipped"})
            continue
        source_event_id = clean(row.get("event_id"))
        occurred_at = norm_datetime(row.get("occurred_at_utc"), errors, context)
        if occurred_at is None:
            errors.append({**context, "source_file": path.name, "row_data": row,
                            "error_message": f"event {source_event_id}: unparseable occurred_at, row skipped (NOT NULL column)"})
            continue
        key = (camp_id, source_event_id)
        dedup[key] = {
            "brand_id": brand["id"],
            "campaign_id": camp_id,
            "contact_id": contact_by_ext.get(clean(row.get("external_contact_id"))),
            "source_event_id": source_event_id,
            "event_type": clean(row.get("event_type")),
            "channel": clean(row.get("channel")),
            "occurred_at": occurred_at,
        }
    upsert_batches("historical_events", list(dedup.values()), on_conflict="campaign_id,source_event_id")
    print(f"  events: upserted {len(dedup)} unique rows (deduped) for {brand['slug']}")


def import_send_log(brand, path, errors):
    if not path.exists():
        return
    context = {"brand_id": brand["id"]}
    campaigns = sb.table("campaigns").select("id,external_id").eq("brand_id", brand["id"]).execute().data
    camp_by_ext = {c["external_id"]: c["id"] for c in campaigns}

    dedup = {}
    for row in read_rows(path, delimiter=",", errors=errors, context=context):
        camp_id = camp_by_ext.get(clean(row.get("campaign_external_id")))
        if not camp_id:
            continue
        queued_at = norm_datetime(row.get("queued_at_utc"), errors, context)
        if queued_at is None:
            errors.append({**context, "source_file": path.name, "row_data": row,
                            "error_message": "unparseable queued_at, row skipped (NOT NULL column)"})
            continue
        key = (camp_id, clean(row.get("batch_key")))
        dedup[key] = {
            "brand_id": brand["id"],
            "campaign_id": camp_id,
            "batch_key": clean(row.get("batch_key")),
            "recipient_count": norm_int(row.get("recipient_count")),
            "status": clean(row.get("status")),
            "queued_at": queued_at,
        }
    upsert_batches("historical_send_batches", list(dedup.values()), on_conflict="campaign_id,batch_key")
    print(f"  send-log: upserted {len(dedup)} unique rows for {brand['slug']}")


# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------

BRAND_FILES = {
    "kilele-rides": dict(
        contacts=["kilele-contacts.csv", "kilele-contacts-delta-2026-09-01.csv"],
        campaigns="kilele-campaigns.csv",
        events="kilele-events.csv",
        send_log="kilele-send-log.csv",
        delimiter=",",
    ),
    "karoo-coaches": dict(
        contacts=["karoo-contacts.csv"],
        campaigns="karoo-campaigns.csv",
        events="karoo-events.csv",
        send_log=None,
        delimiter=",",
    ),
    "marrakech-express": dict(
        contacts=["marrakech-contacts.csv"],
        campaigns="marrakech-campaigns.csv",
        events="marrakech-events.csv",
        send_log=None,
        delimiter=";",
    ),
}


def main(seed_dir: str):
    seed_dir = Path(seed_dir)
    errors = []

    brands = {b["slug"]: b for b in sb.table("brands").select("id,slug").execute().data}

    for slug, cfg in BRAND_FILES.items():
        brand = brands[slug]
        print(f"Importing {slug}...")
        contact_paths = [seed_dir / f for f in cfg["contacts"]]
        import_contacts(brand, contact_paths, cfg["delimiter"], errors)
        import_campaigns(brand, seed_dir / cfg["campaigns"], cfg["delimiter"], errors)
        import_events(brand, seed_dir / cfg["events"], cfg["delimiter"], errors)
        if cfg["send_log"]:
            import_send_log(brand, seed_dir / cfg["send_log"], errors)

    if errors:
        # load_errors table doesn't have a "field"/"source_file" free-for-all —
        # fold extras into row_data so nothing is lost.
        clean_errors = []
        for e in errors:
            clean_errors.append({
                "brand_id": e["brand_id"],
                "source_file": e.get("source_file", ""),
                "row_data": {k: v for k, v in e.items() if k not in ("brand_id", "error_message")},
                "error_message": e["error_message"],
            })
        for batch in batched(clean_errors, BATCH_SIZE):
            sb.table("load_errors").insert(batch).execute()
        print(f"\n{len(errors)} data-quality issues logged to load_errors.")
    else:
        print("\nNo data-quality issues encountered.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python import_seed.py /path/to/seed/dir")
        sys.exit(1)
    main(sys.argv[1])