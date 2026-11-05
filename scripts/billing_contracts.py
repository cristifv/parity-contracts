#!/usr/bin/env python3
"""Writes parity-billing's Kafka contracts (parity-billing PLAN.md §4.2–§4.5) and admin-console's
provisioning contract (PLAN.md §2.5, §4.3):
- schemas/billing/billing-requests.v1.json: commands on billing.web.requests ("x-web": parity-frontend),
  billing.requests (unmarked: admin-console's reads) and billing.admin.requests ("x-admin"); ping on all three;
- schemas/billing/billing-replies.v1.json: parity-billing's answers on replies.parity-frontend and
  replies.admin-console;
- schemas/billing/billing-events.v1.json: billing.events (bound tenants only);
- schemas/billing/billing-entitlements.v1.json: billing.entitlements (compacted; null = tombstone);
- schemas/admin/provisioning-requests.v1.json, provisioning-replies.v1.json: admin.provisioning.requests
  (written by parity-billing only) and admin-console's answers on replies.parity-billing;
- fixtures/billing.v1/: valid and invalid records (kind "billing");
- tenants/test-entitlements.v1.json: the shared test tenants' entitlements (additions only);
- masking/billing.v1.json: the email class and the billing secrets (cases.v1.json is unchanged);
- the "billing" kind in schemas/fixtures/fixture.v1.json.
Run from anywhere: python3 -I scripts/billing_contracts.py. Output is deterministic.
"""
import json
import pathlib
import shutil

ROOT = pathlib.Path(__file__).resolve().parent.parent
TRACE = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
T0 = 1792800000000
DAY = 86_400_000

TENANT_ID = {"type": "string", "pattern": "^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"}
SLUG = {"type": "string", "minLength": 3, "maxLength": 40, "pattern": "^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
        "description": "A requested tenant id: the tenant_id format, 3 to 40 characters"}
REQUEST_ID = {"type": "string", "pattern": "^[A-Za-z0-9._:-]{1,200}$"}
REPLY_TO = {"type": "string", "pattern": "^replies\\.[a-z0-9][a-z0-9.-]{0,200}$"}
UUID = {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}
MS = {"type": "integer", "minimum": 0}
NULLABLE_MS = {"type": ["integer", "null"], "minimum": 0}
VERSION = {"type": "integer", "minimum": 1}
COUNT = {"type": "integer", "minimum": 0}
MINOR = {"type": "integer", "minimum": 0, "description": "An amount in the currency's minor unit (cents)"}
CURRENCY = {"type": "string", "pattern": "^[A-Z]{3}$", "description": "ISO 4217"}
COUNTRY = {"type": "string", "pattern": "^[A-Z]{2}$", "description": "ISO 3166-1 alpha-2"}
REGION = {"type": "string", "pattern": "^[a-z]{2,8}[0-9]{1,3}$", "description": "A region, e.g. eu1"}
LOCALE = {"type": "string", "pattern": "^[a-z]{2}(?:-[A-Z]{2})?$"}
NAME = {"type": "string", "minLength": 1, "maxLength": 200, "pattern": "^[^\u0000-\u001f\u007f]+$"}
EMAIL = {"type": "string", "minLength": 3, "maxLength": 254, "pattern": "^[^@\\s\u0000-\u001f]{1,64}@[^@\\s\u0000-\u001f]{1,253}$",
         "x-pii": "email"}
PLAN_ID = {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,63}$"}
VAT_ID = {"type": "string", "minLength": 4, "maxLength": 20, "pattern": "^[A-Za-z0-9 .-]+$"}
DATE = {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$", "description": "A UTC day, YYYY-MM-DD"}
SESSION_ID = {"type": "string", "pattern": "^cs_[A-Za-z0-9_]{1,250}$", "description": "A Stripe Checkout Session id"}
HOLDER = {"type": "string", "pattern": "^(?:signup:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|onboarding:[A-Za-z0-9._:-]{1,120})$",
          "description": "Who holds a reservation: a self-serve signup, or an operator's onboarding"}
KIND = {"enum": ["trial", "paid"]}
SIGNUP_STATES = ["verified", "reserved", "checkout_open", "paid", "card_saved", "trial_refused", "review",
                 "provisioning", "bound", "ready", "needs_new_slug", "stuck"]
ENTITLEMENT_STATES = ["trial", "trial_ended", "active", "grace", "restricted", "suspended", "cancelled"]
TIERS = ["T0", "T1", "T2", "T3"]
NUMBER_TYPES = ["geographic", "mobile", "toll_free", "premium", "international"]
CHANNELS = ["voice", "webrtc", "chat", "sms", "whatsapp", "email", "social", "video"]


def obj(required, props, **extra):
    d = {"type": "object", "required": list(required), "properties": props}
    d.update(extra)
    return d


def strict(required, props, **extra):
    return obj(required, props, additionalProperties=False, **extra)


def schema_doc(sid, title, description, body):
    doc = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": sid, "title": title,
           "description": description}
    doc.update(body)
    return doc


# ---- billing requests -------------------------------------------------------------------------

DOC_VERSION = {"type": "string", "pattern": "^[A-Za-z0-9._-]{1,40}$"}
PLAN_CHOICE = strict(["kind", "plan_id"], {
    "kind": KIND, "plan_id": PLAN_ID,
    "seats": {"type": ["integer", "null"], "minimum": 1, "maximum": 50,
              "description": "Null or absent: the trial's cap, or the plan's minimum"}})
CONSENTS = strict(["terms", "privacy", "dpa"], {
    "terms": DOC_VERSION, "privacy": DOC_VERSION, "dpa": DOC_VERSION, "aup": DOC_VERSION},
    description="The versions of the legal texts accepted")

# cmd: (mark, required args, args): mark "web" (billing.web.requests), "admin" (billing.admin.requests),
# None (billing.requests); ping is answered on all three.
COMMANDS = {
    "ping": (None, [], {}),
    "get_entitlements": (None, ["tenant_id"], {"tenant_id": TENANT_ID}),
    "get_catalog": ("web", [], {}),
    "check_slug": ("web", ["slug"], {"slug": SLUG}),
    "start_signup": ("web", ["signup_id", "email", "name", "company", "country", "slug", "region", "plan",
                             "consents", "consented_at_ms"],
                     {"signup_id": UUID, "email": EMAIL, "name": NAME, "company": NAME, "country": COUNTRY,
                      "vat_id": {"anyOf": [VAT_ID, {"type": "null"}]}, "slug": SLUG,
                      "slug_review": {"type": "boolean", "description": "The slug is on the brand list: a person reviews it"},
                      "region": REGION, "plan": PLAN_CHOICE, "locale": LOCALE,
                      "consents": CONSENTS, "consented_at_ms": MS, "marketing_opt_in": {"type": "boolean"},
                      "verified_at_ms": MS}),
    "get_signup_status": ("web", ["signup_id"], {"signup_id": UUID}),
    "change_slug": ("web", ["signup_id", "slug"], {"signup_id": UUID, "slug": SLUG}),
    "create_signup_checkout": ("web", ["signup_id"], {
        "signup_id": UUID, "kind": KIND, "plan_id": PLAN_ID,
        "seats": {"type": "integer", "minimum": 1, "maximum": 50}}),
    "confirm_checkout": ("web", ["signup_id", "checkout_session_id"], {"signup_id": UUID, "checkout_session_id": SESSION_ID}),
}
RESERVED_COMMANDS = [
    # billing.web.requests (later steps: B2, F3, B4, B5)
    "get_workspace", "resend_admin_invite", "get_account", "list_ledger_entries", "create_upgrade_checkout",
    "preview_seat_change", "change_seats", "create_portal_session", "start_identity_verification",
    "cancel_subscription", "undo_cancel", "create_topup_checkout", "set_auto_recharge",
    # billing.requests
    "list_provisionings",
    # billing.admin.requests
    "set_contract_entitlements", "set_fraud_tier", "restrict_tenant", "unrestrict_tenant", "approve_action",
    "approve_review", "reject_review", "refund_signup", "retry_provisioning", "adjust_credit",
]


def requests_schema():
    defs = {}
    for cmd, (mark, required, args) in COMMANDS.items():
        d = obj(["cmd", *required], {"cmd": {"const": cmd}, **args})
        if mark == "web":
            d["x-web"] = True
        elif mark == "admin":
            d["x-admin"] = True
        defs["cmd_" + cmd] = d
    return schema_doc(
        "https://parity.example/schemas/billing-requests/v1.json",
        "parity-billing commands, schema_version 1",
        "On three topics; the topic decides what may run (CONVENTIONS.md 'Billing API'). billing.web.requests "
        "(written by parity-frontend only, by Kafka ACL) runs the commands marked x-web, and its only reply_to is "
        "replies.parity-frontend. billing.requests (admin-console) runs the unmarked reads; billing.admin.requests "
        "(admin-console) the commands marked x-admin. ping runs on all three. A command on the wrong topic is "
        "answered forbidden. Signup commands come before any tenant exists: keyed by signup_id, no tenant_id "
        "header. x-reserved names commands of later steps.",
        {"type": "object", "required": ["schema_version", "cmd", "id", "reply_to"],
         "properties": {"schema_version": {"const": 1}, "cmd": {"type": "string"}, "id": REQUEST_ID,
                        "reply_to": REPLY_TO},
         "oneOf": [{"$ref": f"#/$defs/cmd_{c}"} for c in COMMANDS],
         "unevaluatedProperties": False,
         "x-reserved": RESERVED_COMMANDS,
         "$defs": defs})


# ---- entitlements -------------------------------------------------------------------------------

ENTITLEMENT = strict(
    ["schema_version", "tenant_id", "version", "state", "reason", "plan_id", "tier", "seats", "numbers", "channels",
     "cpaas", "outbound", "messaging", "email", "caps", "retention_max_days", "trial_credit_pct", "period_end_ms",
     "computed_at_ms"],
    {"schema_version": {"const": 1}, "tenant_id": TENANT_ID, "version": VERSION,
     "state": {"enum": ENTITLEMENT_STATES},
     "reason": {"type": "string", "pattern": "^[a-z0-9_.:-]{1,80}$",
                "description": "Why this version: plan, trial_credit, dunning, gate, override:<action id>"},
     "plan_id": {"anyOf": [PLAN_ID, {"type": "null"}]},
     "tier": {"enum": TIERS},
     "seats": COUNT,
     "numbers": strict(["max", "types"], {"max": COUNT, "types": {
         "type": "array", "uniqueItems": True, "items": {"enum": NUMBER_TYPES}}},
         description="P4: the allowance and the number types that may be bought"),
     "channels": {"type": "array", "uniqueItems": True, "items": {"enum": CHANNELS}},
     "cpaas": {"type": "boolean", "description": "P6"},
     "outbound": strict(["pstn", "from_inbound", "countries", "international"], {
         "pstn": {"type": "boolean", "description": "P1"},
         "from_inbound": {"type": "boolean", "description": "P2: PSTN legs added to an inbound call"},
         "countries": {"type": "array", "uniqueItems": True, "items": COUNTRY},
         "international": {"type": "boolean"}}),
     "messaging": strict(["sms", "whatsapp"], {"sms": {"type": "boolean"}, "whatsapp": {"type": "boolean"}},
                         description="P3"),
     "email": strict(["outbound", "daily_max"], {"outbound": {"type": "boolean"}, "daily_max": COUNT},
                     description="P9 (FE-026): outbound email from the platform (the email channel, campaigns)"),
     "caps": strict(["daily_spend_minor", "currency", "concurrent_outbound", "cps", "concurrent_inbound",
                     "inbound_minutes_month", "ai_minutes_month", "max_interaction_s"], {
         "daily_spend_minor": MINOR, "currency": CURRENCY, "concurrent_outbound": COUNT, "cps": COUNT,
         "concurrent_inbound": {"type": "integer", "minimum": 1, "description": "P5: never 0 (callers are never silenced by machine)"},
         "inbound_minutes_month": {"type": "integer", "minimum": 1, "description": "P5, pooled per tenant"},
         "ai_minutes_month": {"type": "integer", "minimum": 0, "description": "P7"},
         "max_interaction_s": {"type": ["integer", "null"], "minimum": 60,
                               "description": "FE-027: the longest interaction allowed (trials); null: no cap"}}),
     "retention_max_days": {"type": "integer", "minimum": 1,
                            "description": "P8: the ceiling for new recordings; existing ones follow the deletion policy and the suspension countdown, never a lowered ceiling"},
     "trial_credit_pct": {"type": ["integer", "null"], "minimum": 0, "maximum": 100,
                          "description": "Trial credit left, percent (banners at 50 and 20); null: no trial"},
     "period_end_ms": NULLABLE_MS,
     "computed_at_ms": MS})


def entitlements_schema():
    return schema_doc(
        "https://parity.example/schemas/billing-entitlements/v1.json",
        "billing.entitlements, schema_version 1",
        "What a bound tenant may use: the commercial ceilings the console and the zone services enforce "
        "(CONVENTIONS.md 'Entitlements'). Compacted, keyed tenant:<tenant_id>; a null value is a tombstone. "
        "Headers tenant_id, schema_version, version (on tombstones too) and traceparent. version goes up by one "
        "per change; readers keep the highest and fail closed.",
        {"$ref": "#/$defs/entitlement", "$defs": {"entitlement": ENTITLEMENT}})


# ---- replies ----------------------------------------------------------------------------------------

FEATURES = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 200}}
AVAILABLE = {"type": "boolean", "description": "False while the plan's Stripe prices drift from the catalog"}
TRIAL_PLAN = strict(["plan_id", "kind", "name", "available", "features", "trial"], {
    "plan_id": PLAN_ID, "kind": {"const": "trial"}, "name": NAME, "available": AVAILABLE, "features": FEATURES,
    "trial": strict(["credit_minor", "currency", "days", "max_seats"], {
        "credit_minor": MINOR, "currency": CURRENCY, "days": {"type": "integer", "minimum": 1},
        "max_seats": {"type": "integer", "minimum": 1}})})
PAID_PLAN = strict(["plan_id", "kind", "name", "available", "features", "seat_price_minor", "currency", "interval",
                    "min_seats", "max_seats"], {
    "plan_id": PLAN_ID, "kind": {"const": "paid"}, "name": NAME, "available": AVAILABLE, "features": FEATURES,
    "seat_price_minor": MINOR, "currency": CURRENCY, "interval": {"enum": ["month"]},
    "min_seats": {"type": "integer", "minimum": 1}, "max_seats": {"type": "integer", "minimum": 1}})

SIGNUP_VIEW = strict(["signup_id", "state", "slug", "kind", "plan_id", "seats", "trial", "next", "updated_at_ms"], {
    "signup_id": UUID, "state": {"enum": SIGNUP_STATES}, "slug": SLUG, "kind": KIND, "plan_id": PLAN_ID,
    "seats": {"type": "integer", "minimum": 1},
    "trial": {"anyOf": [strict(["granted", "credit_minor", "currency", "expires_at_ms", "refused"], {
        "granted": {"type": "boolean"}, "credit_minor": MINOR, "currency": CURRENCY, "expires_at_ms": NULLABLE_MS,
        "refused": {"anyOf": [{"enum": ["already_used", "card_not_allowed", "not_eligible"]}, {"type": "null"}]}}),
        {"type": "null"}]},
    "next": {"enum": ["checkout", "wait", "pick_slug", "choose_paid", "done", "contact_support"],
             "description": "The page's one next action"},
    "updated_at_ms": MS})

BILLING_RESULTS = {
    "result_ping": strict(["pong"], {"pong": {"const": True}}),
    "result_catalog": strict(["version", "currency", "plans"], {
        "version": VERSION, "currency": CURRENCY,
        "plans": {"type": "array", "items": {"oneOf": [TRIAL_PLAN, PAID_PLAN]}}}),
    "result_slug": strict(["slug", "available", "reason"], {
        "slug": SLUG, "available": {"type": ["boolean", "null"], "description": "A hint only; null: not checked"},
        "reason": {"anyOf": [{"enum": ["taken", "reserved_word", "unchecked"]}, {"type": "null"}]}}),
    "result_signup": SIGNUP_VIEW,
    "result_checkout": strict(["signup_id", "kind", "url", "expires_at_ms"], {
        "signup_id": UUID, "kind": KIND,
        "url": {"type": "string", "pattern": "^https://checkout\\.stripe\\.com/[^\\s]{1,2000}$", "x-secret": True,
                "description": "The hosted Stripe Checkout page; never logged"},
        "expires_at_ms": MS}),
    "result_entitlements": strict(["entitlements"], {"entitlements": {"$ref": "#/$defs/entitlement"}}),
}
BILLING_ERROR_CODES = ["bad_request", "forbidden", "not_found", "conflict", "unavailable", "internal"]
BILLING_ERROR_RULES = ["slug_taken", "reserved_word", "wrong_state", "plan_unknown", "plan_unavailable", "trial_seats",
                       "trial_unavailable", "expired", "session_mismatch", "reservation_unavailable",
                       "stripe_unavailable"]


def error_def(codes, rules):
    return strict(["code", "message"], {"code": {"enum": codes}, "message": {"type": "string", "maxLength": 500},
                                        "rule": {"enum": rules}})


def replies_doc(sid, title, description, results, extra_defs, codes, rules):
    return schema_doc(sid, title, description, {
        "type": "object", "required": ["schema_version", "id", "ok"],
        "properties": {
            "schema_version": {"const": 1}, "id": REQUEST_ID, "ok": {"type": "boolean"},
            "result": {"anyOf": [{"$ref": f"#/$defs/{k}"} for k in results]},
            "error": error_def(codes, rules)},
        "if": {"properties": {"ok": {"const": True}}},
        "then": {"required": ["result"], "not": {"required": ["error"]}},
        "else": {"required": ["error"], "not": {"required": ["result"]}},
        "unevaluatedProperties": False,
        "$defs": {**extra_defs, **results}})


def replies_schema():
    return replies_doc(
        "https://parity.example/schemas/billing-replies/v1.json",
        "parity-billing answers, schema_version 1",
        "On the request's reply_to (replies.parity-frontend for billing.web.requests, replies.admin-console for "
        "the console's topics), keyed like the request, headers schema_version and traceparent. message is for "
        "people: no personal data, never another tenant's. rule names the broken rule.",
        BILLING_RESULTS, {"entitlement": ENTITLEMENT}, BILLING_ERROR_CODES,
        BILLING_ERROR_RULES)


# ---- events ---------------------------------------------------------------------------------------

EVENTS = {
    "tenant.signed_up": (["plan_id", "kind"], {"plan_id": PLAN_ID, "kind": KIND}),
    "subscription.changed": (["subscription_id", "status", "plan_id", "seats", "period_end_ms"], {
        "subscription_id": {"type": "string", "pattern": "^sub_[A-Za-z0-9]{1,250}$"},
        "status": {"enum": ["incomplete", "incomplete_expired", "trialing", "active", "past_due", "canceled",
                            "unpaid", "paused"]},
        "plan_id": PLAN_ID, "seats": COUNT, "period_end_ms": NULLABLE_MS}),
    "invoice.paid": (["invoice_id", "amount_minor", "currency"], {
        "invoice_id": {"type": "string", "pattern": "^in_[A-Za-z0-9]{1,250}$"}, "amount_minor": MINOR,
        "currency": CURRENCY}),
    "invoice.payment_failed": (["invoice_id", "amount_minor", "currency", "attempt"], {
        "invoice_id": {"type": "string", "pattern": "^in_[A-Za-z0-9]{1,250}$"}, "amount_minor": MINOR,
        "currency": CURRENCY, "attempt": {"type": "integer", "minimum": 1}}),
    "dunning.changed": (["state", "previous"], {"state": {"enum": ENTITLEMENT_STATES},
                                                "previous": {"enum": ENTITLEMENT_STATES}}),
}
RESERVED_EVENTS = ["trial.credit_low", "trial.ended", "credit.added", "credit.reversed", "fraud.flagged",
                   "fraud.cleared", "identity.verified"]


def events_schema():
    defs = {}
    for t, (required, props) in EVENTS.items():
        defs["type_" + t.replace(".", "_")] = obj(["type", *required], {"type": {"const": t}, **props})
    return schema_doc(
        "https://parity.example/schemas/billing-events/v1.json",
        "billing.events, schema_version 1",
        "Money events of bound tenants only (nothing before binding, PLAN M1). Keyed by tenant_id, headers "
        "tenant_id, schema_version, traceparent. Amounts in minor units with the currency; no personal data. "
        "Published through parity-billing's outbox: at least once; drop an event_id already applied. "
        "x-reserved names the types of later steps.",
        {"type": "object", "required": ["schema_version", "event_id", "type", "at_ms", "tenant_id"],
         "properties": {"schema_version": {"const": 1}, "event_id": UUID, "type": {"type": "string"},
                        "at_ms": MS, "tenant_id": TENANT_ID},
         "oneOf": [{"$ref": "#/$defs/type_" + t.replace(".", "_")} for t in EVENTS],
         "unevaluatedProperties": False,
         "x-reserved": RESERVED_EVENTS,
         "$defs": defs})


# ---- provisioning (admin-console's) -----------------------------------------------------------------

PROVISIONING_COMMANDS = {
    "check_tenant_id": (["tenant_id"], {"tenant_id": SLUG}),
    "reserve_tenant_id": (["tenant_id", "holder", "ttl_s"], {
        "tenant_id": SLUG, "holder": HOLDER, "ttl_s": {"type": "integer", "minimum": 60, "maximum": 604800}}),
    "release_tenant_id": (["tenant_id", "holder"], {"tenant_id": SLUG, "holder": HOLDER}),
    "provision_tenant": (["signup_id", "tenant_id", "name", "tier", "region", "admin", "locale", "country"], {
        "signup_id": UUID, "tenant_id": SLUG, "name": NAME, "tier": {"enum": ["small"]}, "region": REGION,
        "admin": strict(["email", "name"], {"email": EMAIL, "name": NAME}), "locale": LOCALE, "country": COUNTRY}),
    "get_provisioning": (["signup_id"], {"signup_id": UUID}),
    "get_seat_usage": (["tenant_id", "day"], {"tenant_id": TENANT_ID, "day": DATE}),
    "resend_admin_invite": (["signup_id"], {"signup_id": UUID}),
}

PROVISIONING_RESULTS = {
    "result_tenant_id": strict(["tenant_id", "available", "reason"], {
        "tenant_id": SLUG, "available": {"type": "boolean"},
        "reason": {"anyOf": [{"enum": ["taken", "reserved", "reserved_word", "tombstoned"]}, {"type": "null"}]}}),
    "result_reservation": strict(["tenant_id", "holder", "expires_at_ms"], {
        "tenant_id": SLUG, "holder": HOLDER, "expires_at_ms": MS}),
    "result_release": strict(["tenant_id", "released"], {"tenant_id": SLUG, "released": {"type": "boolean"}}),
    "result_provisioning": strict(["signup_id", "status", "tenant_id", "username", "steps"], {
        "signup_id": UUID, "status": {"enum": ["in_progress", "created"]}, "tenant_id": SLUG,
        "username": {"anyOf": [{"type": "string", "pattern": "^[a-z0-9][a-z0-9._-]{0,99}$"}, {"type": "null"}],
                     "description": "The first admin's username (login with email is off); set when created"},
        "steps": {"type": "array", "items": strict(["name", "done"], {
            "name": {"enum": ["put_tenant", "set_placement", "organization", "desktop_client", "first_admin",
                              "required_actions", "default_config", "audit"]},
            "done": {"type": "boolean"}})}}),
    "result_seat_usage": strict(["tenant_id", "day", "enabled_now", "max_enabled_in_day"], {
        "tenant_id": TENANT_ID, "day": DATE, "enabled_now": COUNT,
        "max_enabled_in_day": {"type": "integer", "minimum": 0,
                               "description": "FE-027: the most agents enabled at any time during the UTC day"}}),
    "result_invite": strict(["signup_id", "sent", "retry_after_ms"], {
        "signup_id": UUID, "sent": {"type": "boolean"}, "retry_after_ms": NULLABLE_MS}),
}
PROVISIONING_ERROR_RULES = ["taken", "not_reserved", "held_by_other", "rate_limited"]


def provisioning_requests_schema():
    defs = {}
    for cmd, (required, args) in PROVISIONING_COMMANDS.items():
        defs["cmd_" + cmd] = obj(["cmd", *required], {"cmd": {"const": cmd}, **args})
    return schema_doc(
        "https://parity.example/schemas/provisioning-requests/v1.json",
        "admin-console provisioning commands, schema_version 1",
        "On admin.provisioning.requests, written by parity-billing only (Kafka ACL); admin-console owns the "
        "tenant-id namespace and creates tenants (CONVENTIONS.md 'Provisioning'). Keyed by the tenant id the "
        "command names, else signup_id. The only reply_to is replies.parity-billing.",
        {"type": "object", "required": ["schema_version", "cmd", "id", "reply_to"],
         "properties": {"schema_version": {"const": 1}, "cmd": {"type": "string"}, "id": REQUEST_ID,
                        "reply_to": REPLY_TO},
         "oneOf": [{"$ref": f"#/$defs/cmd_{c}"} for c in PROVISIONING_COMMANDS],
         "unevaluatedProperties": False,
         "$defs": defs})


def provisioning_replies_schema():
    return replies_doc(
        "https://parity.example/schemas/provisioning-replies/v1.json",
        "admin-console provisioning answers, schema_version 1",
        "On replies.parity-billing, keyed like the request. get_provisioning answers not_found when there is no "
        "intent for the signup (billing then sends provision_tenant again).",
        PROVISIONING_RESULTS, {}, BILLING_ERROR_CODES, PROVISIONING_ERROR_RULES)


# ---- fixtures ---------------------------------------------------------------------------------------

SIGNUP = "0192f6a1-e000-7aaa-8bbb-00000000a001"
SIGNUP2 = "0192f6a1-e000-7aaa-8bbb-00000000a002"
WEB_ARGS = {
    "ping": {}, "get_entitlements": {"tenant_id": "acme"}, "get_catalog": {}, "check_slug": {"slug": "acme-new"},
    "start_signup": {"signup_id": SIGNUP, "email": "pat@acme-new.example", "name": "Pat Example",
                     "company": "Acme New Ltd", "country": "DE", "vat_id": "DE123456789", "slug": "acme-new",
                     "slug_review": False, "region": "eu1", "plan": {"kind": "trial", "plan_id": "trial", "seats": 2},
                     "locale": "en", "consents": {"terms": "2026-10", "privacy": "2026-10", "dpa": "2026-10", "aup": "2026-10"},
                     "consented_at_ms": T0, "marketing_opt_in": False},
    "get_signup_status": {"signup_id": SIGNUP}, "change_slug": {"signup_id": SIGNUP, "slug": "acme-two"},
    "create_signup_checkout": {"signup_id": SIGNUP},
    "confirm_checkout": {"signup_id": SIGNUP, "checkout_session_id": "cs_test_a1b2c3"},
}
REPLY_TOPIC = {"web": "replies.parity-frontend", None: "replies.admin-console", "admin": "replies.admin-console"}
REQUEST_TOPIC = {"web": "billing.web.requests", None: "billing.requests", "admin": "billing.admin.requests"}


def request(name, topic=None, key=None, **override):
    mark = COMMANDS[name][0]
    topic = topic or REQUEST_TOPIC[mark]
    reply_to = "replies.parity-frontend" if topic == "billing.web.requests" else "replies.admin-console"
    rid = ("parity-frontend-1:" if topic == "billing.web.requests" else "admin-api-1:") + name.replace("_", "-") + "-0001"
    value = {"schema_version": 1, "cmd": name, "id": rid, "reply_to": reply_to, **WEB_ARGS[name]}
    for k, v in override.items():
        if v is None:
            value.pop(k, None)
        else:
            value[k] = v
    if key is None:
        key = value.get("tenant_id") or value.get("signup_id") or rid
    return {"topic": topic, "key": key, "headers": {"traceparent": TRACE}, "value": value}


def reply(result=None, error=None, topic="replies.parity-frontend", rid="parity-frontend-1:get-catalog-0001"):
    v = {"schema_version": 1, "id": rid, "ok": error is None}
    if error is None:
        v["result"] = result
    else:
        v["error"] = error
    return {"topic": topic, "key": rid, "headers": {"schema_version": "1", "traceparent": TRACE}, "value": v}


def entitlement(tenant="acme", **override):
    e = {"schema_version": 1, "tenant_id": tenant, "version": 3, "state": "active", "reason": "plan",
         "plan_id": "team", "tier": "T1", "seats": 10, "numbers": {"max": 2, "types": ["geographic"]},
         "channels": ["voice", "webrtc", "chat"], "cpaas": False,
         "outbound": {"pstn": False, "from_inbound": False, "countries": [], "international": False},
         "messaging": {"sms": False, "whatsapp": False}, "email": {"outbound": False, "daily_max": 0},
         "caps": {"daily_spend_minor": 2500, "currency": "EUR", "concurrent_outbound": 0, "cps": 0,
                  "concurrent_inbound": 10, "inbound_minutes_month": 10000, "ai_minutes_month": 300,
                  "max_interaction_s": None},
         "retention_max_days": 90, "trial_credit_pct": None, "period_end_ms": T0 + 30 * DAY, "computed_at_ms": T0}
    e.update(override)
    return e


TRIAL_ENTITLEMENT = entitlement(
    "globex", version=1, state="trial", reason="trial_credit", plan_id="trial", tier="T0", seats=3,
    numbers={"max": 0, "types": []}, channels=["webrtc", "chat"],
    caps={"daily_spend_minor": 0, "currency": "EUR", "concurrent_outbound": 0, "cps": 0, "concurrent_inbound": 3,
          "inbound_minutes_month": 3000, "ai_minutes_month": 30, "max_interaction_s": 1200},
    retention_max_days=30, trial_credit_pct=100, period_end_ms=T0 + 30 * DAY)


def ent_record(value, key=None, version=None, tenant_header=True):
    tenant = value["tenant_id"] if value else "acme"
    headers = {"schema_version": "1", "version": str(value["version"] if value else (version or 4)),
               "traceparent": TRACE}
    if tenant_header:
        headers["tenant_id"] = tenant
    return {"topic": "billing.entitlements", "key": key or f"tenant:{tenant}", "headers": headers, "value": value}


def event(etype, **body):
    v = {"schema_version": 1, "event_id": "0192f6a1-e000-7aaa-8bbb-000000000b01", "type": etype, "at_ms": T0,
         "tenant_id": "acme", **body}
    return {"topic": "billing.events", "key": "acme",
            "headers": {"tenant_id": "acme", "schema_version": "1", "traceparent": TRACE}, "value": v}


EVENT_BODIES = {
    "tenant.signed_up": {"plan_id": "team", "kind": "paid"},
    "subscription.changed": {"subscription_id": "sub_1Abc", "status": "active", "plan_id": "team", "seats": 10,
                             "period_end_ms": T0 + 30 * DAY},
    "invoice.paid": {"invoice_id": "in_1Abc", "amount_minor": 60000, "currency": "EUR"},
    "invoice.payment_failed": {"invoice_id": "in_1Abd", "amount_minor": 60000, "currency": "EUR", "attempt": 1},
    "dunning.changed": {"state": "grace", "previous": "active"},
}

PROV_ARGS = {
    "check_tenant_id": {"tenant_id": "acme-new"},
    "reserve_tenant_id": {"tenant_id": "acme-new", "holder": f"signup:{SIGNUP}", "ttl_s": 86400},
    "release_tenant_id": {"tenant_id": "acme-new", "holder": f"signup:{SIGNUP}"},
    "provision_tenant": {"signup_id": SIGNUP, "tenant_id": "acme-new", "name": "Acme New Ltd", "tier": "small",
                         "region": "eu1", "admin": {"email": "pat@acme-new.example", "name": "Pat Example"},
                         "locale": "en", "country": "DE"},
    "get_provisioning": {"signup_id": SIGNUP},
    "get_seat_usage": {"tenant_id": "acme", "day": "2026-10-31"},
    "resend_admin_invite": {"signup_id": SIGNUP},
}


def prov_request(name, **override):
    rid = f"parity-billing-1:prov:{SIGNUP}:{name}"
    value = {"schema_version": 1, "cmd": name, "id": rid, "reply_to": "replies.parity-billing", **PROV_ARGS[name]}
    for k, v in override.items():
        if v is None:
            value.pop(k, None)
        else:
            value[k] = v
    key = value.get("tenant_id") or value.get("signup_id") or rid
    return {"topic": "admin.provisioning.requests", "key": key, "headers": {"traceparent": TRACE}, "value": value}


CATALOG = {"version": 1, "currency": "EUR", "plans": [
    {"plan_id": "trial", "kind": "trial", "name": "Trial", "available": True, "features": ["WebRTC test calls and web chat"],
     "trial": {"credit_minor": 1500, "currency": "EUR", "days": 30, "max_seats": 3}},
    {"plan_id": "team", "kind": "paid", "name": "Team", "available": True, "features": ["Voice, WebRTC and chat"],
     "seat_price_minor": 6000, "currency": "EUR", "interval": "month", "min_seats": 1, "max_seats": 50}]}
SIGNUP_VIEW_FIX = {"signup_id": SIGNUP, "state": "card_saved", "slug": "acme-new", "kind": "trial",
                   "plan_id": "trial", "seats": 2,
                   "trial": {"granted": True, "credit_minor": 1500, "currency": "EUR",
                             "expires_at_ms": T0 + 30 * DAY, "refused": None},
                   "next": "wait", "updated_at_ms": T0}


def fixtures():
    valid, invalid = {}, {}
    for cmd, (mark, _, _) in COMMANDS.items():
        valid[f"request-{cmd.replace('_', '-')}"] = (cmd, request(cmd), "apply")
    valid["request-ping-web"] = ("ping on billing.web.requests: answered on every topic",
                                 request("ping", topic="billing.web.requests"), "apply")
    valid["request-ping-admin"] = ("ping on billing.admin.requests", request("ping", topic="billing.admin.requests"),
                                   "apply")
    valid["request-signup-paid"] = ("start_signup for a paid plan, no VAT ID", request(
        "start_signup", vat_id=None, plan={"kind": "paid", "plan_id": "team", "seats": 5}), "apply")
    invalid["request-web-cmd-on-requests"] = (
        "A signup command on billing.requests: forbidden (M2: the web writes only billing.web.requests)",
        request("start_signup", topic="billing.requests"), ["wrong_topic"], "reply_error:forbidden")
    invalid["request-read-on-web"] = (
        "get_entitlements on billing.web.requests: forbidden (no tenant data for the web without a token)",
        request("get_entitlements", topic="billing.web.requests"), ["wrong_topic"], "reply_error:forbidden")
    invalid["request-web-cmd-on-admin"] = ("A web command on billing.admin.requests: forbidden",
                                           request("get_catalog", topic="billing.admin.requests"), ["wrong_topic"],
                                           "reply_error:forbidden")
    invalid["request-web-other-reply-to"] = (
        "billing.web.requests with reply_to replies.other: never answered (rule 2), counted",
        request("get_catalog", reply_to="replies.other"), ["reply_to_not_allowed"], "dlt")
    invalid["request-console-reply-to-frontend"] = (
        "billing.requests answering into replies.parity-frontend: never answered",
        request("ping", reply_to="replies.parity-frontend"), ["reply_to_not_allowed"], "dlt")
    invalid["request-no-reply-to"] = ("No reply_to: never answered", request("get_catalog", reply_to=None),
                                      ["schema", "bad_reply_to"], "dlt")
    invalid["request-reply-to-entitlements"] = ("A reply_to that isn't replies.*: never answered",
                                                request("get_catalog", reply_to="billing.entitlements"),
                                                ["schema", "bad_reply_to"], "dlt")
    invalid["request-no-id"] = ("No id: never answered", request("get_catalog", id=None), ["schema", "bad_id"], "dlt")
    invalid["request-not-json"] = ("A body that isn't JSON: never answered",
                                   {"topic": "billing.web.requests", "key": SIGNUP, "headers": {}},
                                   ["schema", "bad_id", "bad_reply_to"], "dlt")
    invalid["request-bad-slug"] = ("A slug that isn't a DNS label: bad_request", request(
        "check_slug", slug="Acme_Ltd"), ["schema"], "reply_error:bad_request")
    invalid["request-short-slug"] = ("A 2-character slug: bad_request", request("change_slug", slug="ab"),
                                     ["schema"], "reply_error:bad_request")
    invalid["request-unknown-cmd"] = ("An unknown command: bad_request", request("get_catalog", cmd="grant_credit"),
                                      ["schema"], "reply_error:bad_request")
    invalid["request-reserved-cmd"] = ("A reserved command (a later step): bad_request until a release adds it",
                                       request("get_catalog", cmd="get_account"), ["schema"],
                                       "reply_error:bad_request")
    invalid["request-unknown-field"] = ("An unknown field: bad_request", request("get_signup_status", tenant="acme"),
                                        ["schema"], "reply_error:bad_request")
    invalid["request-version-2"] = ("An unknown major: bad_request", request("get_catalog", schema_version=2),
                                    ["schema"], "reply_error:bad_request")
    invalid["request-trial-51-seats"] = ("More seats than any plan has: bad_request", request(
        "start_signup", plan={"kind": "trial", "plan_id": "trial", "seats": 51}), ["schema"], "reply_error:bad_request")
    minimal = request("start_signup", slug_review=None, locale=None, marketing_opt_in=None,
                      plan={"kind": "trial", "plan_id": "trial", "seats": None})
    minimal["value"]["vat_id"] = None
    valid["request-signup-minimal"] = ("start_signup with a null VAT ID, null seats, no slug review (parity-frontend's form)",
                                       minimal, "apply")
    invalid["reply-checkout-not-stripe"] = ("A Checkout URL that isn't Stripe's: the frontend never redirects there", reply(
        {"signup_id": SIGNUP, "kind": "paid", "url": "https://checkout.example.com/pay", "expires_at_ms": T0},
        rid="parity-frontend-1:create-signup-checkout-0003"), ["schema"], "apply")

    valid["reply-catalog"] = ("get_catalog's answer", reply(CATALOG), "apply")
    valid["reply-signup"] = ("A signup's status: card saved, trial credit granted", reply(
        SIGNUP_VIEW_FIX, rid="parity-frontend-1:get-signup-status-0001"), "apply")
    valid["reply-signup-trial-refused"] = ("A repeat trial: no credit, a paid plan only", reply(
        dict(SIGNUP_VIEW_FIX, state="trial_refused", next="choose_paid",
             trial={"granted": False, "credit_minor": 0, "currency": "EUR", "expires_at_ms": None,
                    "refused": "already_used"}), rid="parity-frontend-1:get-signup-status-0002"), "apply")
    valid["reply-signup-expired"] = ("An expired signup: not_found", reply(error={
        "code": "not_found", "message": "this signup has expired", "rule": "expired"},
        rid="parity-frontend-1:get-signup-status-0003"), "apply")
    valid["reply-slug-taken"] = ("change_slug: taken (admin-console's namespace, from B2)", reply(error={
        "code": "conflict", "message": "this workspace name is taken", "rule": "slug_taken"},
        rid="parity-frontend-1:change-slug-0001"), "apply")
    valid["reply-checkout"] = ("create_signup_checkout's answer", reply(
        {"signup_id": SIGNUP, "kind": "trial", "url": "https://checkout.stripe.com/c/pay/cs_test_a1b2c3",
         "expires_at_ms": T0 + DAY}, rid="parity-frontend-1:create-signup-checkout-0001"), "apply")
    valid["reply-slug"] = ("check_slug's answer: a hint", reply(
        {"slug": "acme-new", "available": None, "reason": "unchecked"}, rid="parity-frontend-1:check-slug-0001"),
        "apply")
    valid["reply-pong"] = ("ping's answer", reply({"pong": True}, topic="replies.admin-console",
                                                 rid="admin-api-1:ping-0001"), "apply")
    valid["reply-entitlements"] = ("get_entitlements' answer", reply(
        {"entitlements": entitlement()}, topic="replies.admin-console", rid="admin-api-1:get-entitlements-0001"),
        "apply")
    valid["reply-forbidden"] = ("A command on the wrong topic", reply(error={
        "code": "forbidden", "message": "start_signup is accepted on billing.web.requests only"},
        topic="replies.admin-console", rid="admin-api-1:start-signup-0001"), "apply")
    valid["reply-plan-unavailable"] = ("Catalog drift refuses a checkout", reply(error={
        "code": "conflict", "message": "this plan can't be bought right now", "rule": "plan_unavailable"},
        rid="parity-frontend-1:create-signup-checkout-0002"), "apply")
    invalid["reply-result-and-error"] = ("result and error together", reply(
        {"pong": True}) | {"value": {"schema_version": 1, "id": "x", "ok": True, "result": {"pong": True},
                                     "error": {"code": "internal", "message": "x"}}}, ["schema"], "apply")
    invalid["reply-no-id"] = ("An answer without an id", reply({"pong": True}) | {"value": {
        "schema_version": 1, "ok": True, "result": {"pong": True}}}, ["schema"], "reject")

    for t, body in EVENT_BODIES.items():
        valid["event-" + t.replace(".", "-").replace("_", "-")] = (t, event(t, **body), "apply")
    e = event("invoice.paid", **EVENT_BODIES["invoice.paid"])
    e["key"] = "globex"
    invalid["event-key-not-tenant"] = ("An event keyed by another tenant", e, ["key_mismatch"], "reject")
    e = event("invoice.paid", **EVENT_BODIES["invoice.paid"])
    e["headers"]["tenant_id"] = "globex"
    invalid["event-header-body-mismatch"] = ("The tenant header names another tenant", e, ["header_body_mismatch"],
                                             "reject")
    e = event("invoice.paid", **EVENT_BODIES["invoice.paid"])
    del e["headers"]["tenant_id"]
    invalid["event-missing-tenant-header"] = ("No tenant_id header", e, ["missing_header"], "reject")
    e = event("invoice.paid", **EVENT_BODIES["invoice.paid"])
    e["value"]["amount_minor"] = 600.5
    invalid["event-float-amount"] = ("A float amount: amounts are integers in minor units", e, ["schema"], "reject")
    invalid["event-reserved-type"] = ("A reserved type (a later step)", event("trial.ended"), ["schema"], "reject")

    valid["entitlements-active"] = ("A paid T1 tenant, every gated path closed", ent_record(entitlement()), "apply")
    valid["entitlements-trial"] = ("A trial (T0): seats <= 3, no numbers, capped interactions",
                                   ent_record(TRIAL_ENTITLEMENT), "apply")
    valid["entitlements-tombstone"] = ("A deleted tenant: a tombstone, with its version header",
                                       ent_record(None, version=7), "apply")
    invalid["entitlements-key-mismatch"] = ("Keyed by another tenant", ent_record(entitlement(), key="tenant:globex"),
                                            ["key_mismatch"], "reject")
    invalid["entitlements-bare-key"] = ("A key without the tenant: prefix", ent_record(entitlement(), key="acme"),
                                        ["key_mismatch"], "reject")
    r = ent_record(entitlement())
    r["headers"]["version"] = "2"
    invalid["entitlements-version-header"] = ("The version header differs from the body", r,
                                              ["header_body_mismatch"], "reject")
    invalid["entitlements-no-tenant-header"] = ("No tenant_id header", ent_record(entitlement(), tenant_header=False),
                                                ["missing_header"], "reject")
    invalid["entitlements-zero-inbound"] = ("concurrent_inbound 0: P5 is never a hard cut", ent_record(entitlement(
        caps=dict(entitlement()["caps"], concurrent_inbound=0))), ["schema"], "reject")
    invalid["entitlements-unknown-state"] = ("An unknown state", ent_record(entitlement(state="frozen")), ["schema"],
                                             "reject")
    invalid["entitlements-no-email-field"] = ("No email field (FE-026)", ent_record(
        {k: v for k, v in entitlement().items() if k != "email"}), ["schema"], "reject")

    for cmd in PROVISIONING_COMMANDS:
        valid[f"provisioning-{cmd.replace('_', '-')}"] = (cmd, prov_request(cmd), "apply")
    invalid["provisioning-other-reply-to"] = ("admin.provisioning.requests answered elsewhere: never answered",
                                              prov_request("get_provisioning", reply_to="replies.admin-console"),
                                              ["reply_to_not_allowed"], "dlt")
    invalid["provisioning-tier-mid"] = ("Self-serve provisions small tenants only", prov_request(
        "provision_tenant", tier="mid"), ["schema"], "reply_error:bad_request")
    invalid["provisioning-bad-holder"] = ("A holder that is neither a signup nor an onboarding", prov_request(
        "reserve_tenant_id", holder="someone"), ["schema"], "reply_error:bad_request")
    pr = lambda result=None, error=None: reply(result, error, topic="replies.parity-billing",  # noqa: E731
                                               rid=f"parity-billing-1:prov:{SIGNUP}:get_provisioning")
    valid["provisioning-reply-created"] = ("Created for this signup, with the username", pr(
        {"signup_id": SIGNUP, "status": "created", "tenant_id": "acme-new", "username": "acme-new.admin",
         "steps": [{"name": s, "done": True} for s in ["put_tenant", "set_placement", "organization",
                                                       "desktop_client", "first_admin", "required_actions",
                                                       "default_config", "audit"]]}), "apply")
    valid["provisioning-reply-not-found"] = ("No intent: billing sends provision_tenant again", pr(
        error={"code": "not_found", "message": "no provisioning for this signup"}), "apply")
    valid["provisioning-reply-taken"] = ("reserve_tenant_id: taken", pr(
        error={"code": "conflict", "message": "the tenant id is taken", "rule": "taken"}), "apply")
    valid["provisioning-reply-seat-usage"] = ("get_seat_usage: the day's maximum (FE-027)", pr(
        {"tenant_id": "acme", "day": "2026-10-31", "enabled_now": 2, "max_enabled_in_day": 3}), "apply")
    return valid, invalid


def test_entitlements():
    def full(tenant, **kw):
        return entitlement(tenant, version=1, computed_at_ms=T0, **kw)
    return {
        "schema_version": 1,
        "description": "The shared test tenants' entitlements (parity-billing PLAN §4.5): every reader's tests may "
                       "use them. A release may add to them, never remove one. acme: a paid T1 tenant; globex: a "
                       "mid tenant on contract terms (T3, operator-onboarded); initech-de and initech-uk: "
                       "enterprise sub-tenants (T3). Every gated path stays closed (the gate: enforced.<path> is "
                       "false everywhere until its negative test passes). initech is a parent: no record.",
        "entitlements": [
            full("acme", seats=10),
            full("globex", plan_id="contract", tier="T3", seats=40, reason="override:onboarding",
                 numbers={"max": 10, "types": ["geographic"]}, period_end_ms=None,
                 caps={"daily_spend_minor": 25000, "currency": "EUR", "concurrent_outbound": 0, "cps": 0,
                       "concurrent_inbound": 40, "inbound_minutes_month": 40000, "ai_minutes_month": 2000,
                       "max_interaction_s": None}),
            full("initech-de", plan_id="contract", tier="T3", seats=100, reason="override:onboarding",
                 numbers={"max": 20, "types": ["geographic"]}, period_end_ms=None, retention_max_days=365,
                 caps={"daily_spend_minor": 50000, "currency": "EUR", "concurrent_outbound": 0, "cps": 0,
                       "concurrent_inbound": 100, "inbound_minutes_month": 100000, "ai_minutes_month": 5000,
                       "max_interaction_s": None}),
            full("initech-uk", plan_id="contract", tier="T3", seats=50, reason="override:onboarding",
                 numbers={"max": 10, "types": ["geographic"]}, period_end_ms=None, retention_max_days=365,
                 caps={"daily_spend_minor": 25000, "currency": "GBP", "concurrent_outbound": 0, "cps": 0,
                       "concurrent_inbound": 50, "inbound_minutes_month": 50000, "ai_minutes_month": 2500,
                       "max_interaction_s": None}),
        ]}


def masking_cases():
    return {
        "schema_version": 1,
        "description": "Masking cases for the global billing services (parity-billing, parity-frontend; parity-billing "
                       "PLAN §4.5), on top of cases.v1.json, which every service passes and which stays unchanged "
                       "(its consumers check its classes exactly). Same normalize rules. classify adds the class "
                       "email: the value is replaced by '[email]', whatever it is (0.10.2, FE-044: logs carry ids, "
                       "never an email or a hash of one, keyed or not; a hash is a membership oracle). secret adds the tokens, signatures and the hosted Stripe URLs (they authorise a "
                       "payment page or a portal session). plain lists billing ids that must stay readable.",
        "classify": {
            "email": ["email", "admin_email", "billing_email", "adminEmail", "contact_email"],
            "secret": ["access_token", "client_secret", "stripe_signature", "Stripe-Signature", "webhook_secret",
                       "turnstile_token", "cf_turnstile_response", "checkout_url", "portal_url", "verification_url",
                       "checkoutUrl", "api_key", "secret_key"],
            "plain": ["signup_id", "billing_account_id", "tenant_id", "session_id", "customer_id", "subscription_id",
                      "event_id", "plan_id", "state", "reply_to", "stripe_event_type", "billingAccountId"]},
        "mask_email": [],
    }


def write(rel, doc):
    p = ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def fixture_kind():
    """The 'billing' kind in schemas/fixtures/fixture.v1.json (idempotent)."""
    path = ROOT / "schemas/fixtures/fixture.v1.json"
    doc = json.loads(path.read_text())
    store = doc["$defs"]["store"]
    billing = json.loads(json.dumps(store))
    billing["properties"]["kind"] = {"const": "billing"}
    billing["properties"]["violates"]["items"]["enum"] = list(RULE_CODES)
    billing["properties"]["reader"]["description"] = "What a correct reader does (parity-billing or admin-console for requests; a consumer for the rest)"
    billing["properties"]["record"]["properties"]["value"]["description"] = "The JSON body; absent: not JSON; null: a tombstone (billing.entitlements)"
    doc["$defs"]["billing"] = billing
    ref = {"$ref": "#/$defs/billing"}
    if ref not in doc["oneOf"]:
        doc["oneOf"].append(ref)
    marker = " Since 0.10.0 also a parity-billing record (kind billing), checked against schemas/billing/, schemas/admin/ and CONVENTIONS.md's \"Billing API\", \"Entitlements\" and \"Provisioning\"."
    if marker not in doc["description"]:
        doc["description"] += marker
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


RULE_CODES = ["schema", "bad_id", "bad_reply_to", "reply_to_not_allowed", "wrong_topic", "key_mismatch",
              "header_body_mismatch", "missing_header"]


def main():
    write("schemas/billing/billing-requests.v1.json", requests_schema())
    write("schemas/billing/billing-replies.v1.json", replies_schema())
    write("schemas/billing/billing-events.v1.json", events_schema())
    write("schemas/billing/billing-entitlements.v1.json", entitlements_schema())
    write("schemas/admin/provisioning-requests.v1.json", provisioning_requests_schema())
    write("schemas/admin/provisioning-replies.v1.json", provisioning_replies_schema())
    write("tenants/test-entitlements.v1.json", test_entitlements())
    cases = masking_cases()
    emails = ["Pat@Example.com", " pat@example.com ", "ADMIN@acme-new.example", "x@y.test"]
    cases["mask_email"] = [{"in": e, "out": "[email]"} for e in emails]
    write("masking/billing.v1.json", cases)
    fixture_kind()
    out = ROOT / "fixtures" / "billing.v1"
    if out.exists():
        shutil.rmtree(out)
    valid, invalid = fixtures()
    for name, (description, record, reader) in valid.items():
        write(f"fixtures/billing.v1/valid/{name}.json", {"description": description, "kind": "billing",
                                                          "writer_valid": True, "reader": reader, "record": record})
    for name, (description, record, violates, reader) in invalid.items():
        write(f"fixtures/billing.v1/invalid/{name}.json", {"description": description, "kind": "billing",
                                                            "writer_valid": False, "violates": violates,
                                                            "reader": reader, "record": record})
    print(f"wrote 6 schemas, {len(valid)} valid and {len(invalid)} invalid billing fixtures, "
          "test entitlements, masking/billing.v1.json")


if __name__ == "__main__":
    main()
