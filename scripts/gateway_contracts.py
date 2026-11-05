#!/usr/bin/env python3
"""Writes parity-gateway's contracts that are data, not protos (0.3.0):
- schemas/agent-ws/to-gateway.v1.json and to-desktop.v1.json: the desktop WebSocket frames;
- fixtures/agent-ws.v1/: valid and invalid frames (kind "ws");
- fixtures/agents.v1/ and fixtures/interaction.v1/: gRPC scripts for parity.agents.v1 and
  parity.interaction.v1.
Run from anywhere: python3 -I scripts/gateway_contracts.py. Output is deterministic.
"""
import base64
import hashlib
import json
import pathlib
import shutil
import struct

ROOT = pathlib.Path(__file__).resolve().parent.parent
TRACE = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
TENANT, OTHER = "acme", "globex"
AGENT, AGENT_B = "agent-7f3a", "agent-91c2"
REPLICA, BIN = "gw-bin-a-1", "bin-a"
SESSION, SESSION2 = "0192f6a1-5000-7aaa-8bbb-000000000001", "0192f6a1-5000-7aaa-8bbb-000000000002"
INTERACTION = "0192f6a1-7c3e-7b21-9a4d-5e6f70819203"
OFFER = "0192f6a1-6000-7aaa-8bbb-000000000001"
RES = "0192f6a1-9100-7ccc-8ddd-000000000001"
LEG_A, LEG_B = "0192f6a1-a000-7aaa-8bbb-000000000001", "0192f6a1-a000-7aaa-8bbb-000000000002"
LEG_C = "0192f6a1-a000-7aaa-8bbb-000000000003"
T0 = 1792800000000
CONTACT = "sip:agent-7f3a@acme.agents.example"

UUID = {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}
OPAQUE = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"}
QUEUE = {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,64}$"}
CODE = {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,64}$"}
MS = {"type": "integer", "minimum": 0}
PHONE = {"type": "string", "pattern": "^\\+[1-9][0-9]{6,14}$", "x-pii": "phone"}


def command_id(tenant, agent, frame_id):
    """CONVENTIONS.md "command_id from a desktop": length-prefixed fields, SHA-256, base64url."""
    buf = b"".join(struct.pack(">I", len(f.encode())) + f.encode() for f in (tenant, agent, frame_id))
    return "gw:" + base64.urlsafe_b64encode(hashlib.sha256(buf).digest()).decode().rstrip("=")


def frame(name, props, required, desc):
    return {
        "description": desc,
        "type": "object",
        "additionalProperties": False,
        "required": ["v", "type", *required],
        "properties": {"v": {"const": 1}, "type": {"const": name}, **props},
    }


def schema(direction, frames, desc):
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://parity.example/schemas/agent-ws/{direction}/v1.json",
        "title": f"agent-ws.v1 frames {direction.replace('-', ' ')}",
        "description": desc,
        "oneOf": [{"$ref": f"#/$defs/{n}"} for n in frames],
        "$defs": frames,
    }


# 0.11.0: one queue of an agent's active set (any number of queues and tiers).
MEMBERSHIP = {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["queue_id", "tier"],
    "properties": {"queue_id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,64}$"},
                   "tier": {"enum": ["primary", "secondary"]}, "pinned": {"type": "boolean"},
                   "since_ms": {"type": "integer", "minimum": 0}}}}


def to_gateway():
    cmd = {"id": UUID}
    call = {**cmd, "interaction_id": UUID}
    target = {
        "type": "object", "additionalProperties": False, "minProperties": 1, "maxProperties": 1,
        "properties": {"queue_id": QUEUE, "agent_id": OPAQUE, "number": PHONE},
    }
    f = {
        "auth": frame("auth", {"token": {"type": "string", "minLength": 16, "maxLength": 8192, "x-secret": True},
                               "client_version": {"type": "string", "maxLength": 64}},
                      ["token"], "First frame within 5 s, and again before exp to refresh"),
        "set_state": frame("set_state", {**cmd, "state": {"enum": ["ready", "not_ready", "logged_out"]},
                                         "reason_code": CODE}, ["id", "state"], "RE SetState"),
        "accept": frame("accept", {**cmd, "offer_id": UUID}, ["id", "offer_id"], "RE AnswerOffer(ACCEPT)"),
        "reject": frame("reject", {**cmd, "offer_id": UUID, "reason": CODE}, ["id", "offer_id"], "RE AnswerOffer(REJECT)"),
        "transfer": frame("transfer", {**call, "target": target}, ["id", "interaction_id", "target"], "IE BlindTransfer"),
        "consult": frame("consult", {**call, "target": target}, ["id", "interaction_id", "target"], "IE Consult"),
        "remove_party": frame("remove_party", {**call, "leg_id": UUID}, ["id", "interaction_id", "leg_id"], "IE RemoveParty"),
        "wrap_up_done": frame("wrap_up_done", {**cmd, "reservation_id": UUID, "disposition_code": CODE},
                              ["id", "reservation_id", "disposition_code"], "RE EndWrapUp"),
        "pong": frame("pong", {}, [], "Answers ping"),
    }
    for name in ["hold", "resume", "mute", "unmute", "hangup", "merge", "complete_transfer", "cancel_consult"]:
        f[name] = frame(name, call, ["id", "interaction_id"], f"IE Execute({name})")
    return schema("to-gateway", f, "Desktop -> gateway. No frame names a tenant or an agent: both come from the token. "
                  "id is a UUID the desktop makes per command; the gateway derives command_id from it.")


def to_desktop():
    seq = {"seq": MS}
    agent = {
        "type": "object", "additionalProperties": False, "required": ["state", "state_version", "mode"],
        "properties": {
            "state": {"enum": ["logged_out", "not_ready", "ready", "offered", "reserved", "on_call", "wrap_up"]},
            "reason_code": CODE, "pending_target": {"enum": ["ready", "not_ready", "logged_out"]},
            "pending_reason_code": CODE, "since_ms": MS, "state_version": MS,
            "mode": {"enum": ["inbound", "campaign"]}, "campaign_id": OPAQUE,
            # 0.11.0 (parity-cc-routing phase 4): the active set, the set waiting for the next offer.
            "queues": MEMBERSHIP, "pending_queues": MEMBERSHIP, "queues_version": MS,
        },
    }
    offer = {"offer_id": UUID, "interaction_id": UUID, "channel": {"enum": ["voice", "chat", "sms", "email", "social"]},
             "queue_id": QUEUE, "expires_at_ms": MS, "consult": {"type": "boolean"}, "direct": {"type": "boolean"},
             # 0.11.0: the queue's tier for the agent (absent: direct or escalated), a scheduled callback.
             "tier": {"enum": ["primary", "secondary"]}, "callback": {"type": "boolean"}}
    origin = {"enum": ["inbound", "campaign", "direct", "callback"]}
    reservation = {"reservation_id": UUID, "interaction_id": UUID, "origin": origin, "campaign_id": OPAQUE}
    call = {
        "type": "object", "additionalProperties": False, "required": ["interaction_id", "state", "managed"],
        "properties": {
            "interaction_id": UUID, "reservation_id": UUID,
            "state": {"enum": ["ringing", "connected", "ended"]},
            "held": {"type": "boolean"}, "muted": {"type": "boolean"}, "consulting": {"type": "boolean"},
            "recording": {"enum": ["off", "on", "paused"]}, "managed": {"type": "boolean"},
            "parties": {"type": "array", "maxItems": 16, "items": {
                "type": "object", "additionalProperties": False, "required": ["role", "leg_id", "live"],
                "properties": {"role": {"enum": ["customer", "agent", "supervisor", "external"]},
                               "agent_id": OPAQUE, "leg_id": UUID, "live": {"type": "boolean"}}}},
        },
    }
    obj = lambda props, req: {"type": "object", "additionalProperties": False, "required": req, "properties": props}
    secret = {"type": "string", "minLength": 1, "maxLength": 4096, "x-secret": True}
    f = {
        "snapshot": frame("snapshot", {**seq, "session_id": UUID, "agent": agent,
                                       "offer": obj(offer, ["offer_id", "interaction_id", "expires_at_ms"]),
                                       "reservations": {"type": "array", "items": obj(reservation, ["reservation_id", "interaction_id"])},
                                       "calls": {"type": "array", "items": call},
                                       "wrap_ups": {"type": "array", "items": obj(
                                           {"reservation_id": UUID, "interaction_id": UUID, "ends_at_ms": MS},
                                           ["reservation_id", "ends_at_ms"])}},
                          ["seq", "session_id", "agent", "reservations", "calls", "wrap_ups"],
                          "First frame after auth on every connection: state is authoritative, nothing is replayed"),
        "agent_state": frame("agent_state", {**seq, "agent": agent}, ["seq", "agent"], "RE StateChanged"),
        "offer": frame("offer", {**seq, **offer}, ["seq", "offer_id", "interaction_id", "expires_at_ms"], "RE Offer"),
        "offer_revoked": frame("offer_revoked", {**seq, "offer_id": UUID,
                                                 "reason": {"enum": ["expired", "caller_gone", "detached", "state_changed"]}},
                               ["seq", "offer_id", "reason"], "RE OfferRevoked"),
        "reserved": frame("reserved", {**seq, **reservation}, ["seq", "reservation_id", "interaction_id", "origin"],
                          "RE Reserved: the softphone may auto-answer this interaction's leg"),
        "call": frame("call", {**seq, "call": call}, ["seq", "call"], "IE's view after Get, a control or an event"),
        "wrap_up": frame("wrap_up", {**seq, "reservation_id": UUID, "interaction_id": UUID, "ends_at_ms": MS},
                         ["seq", "reservation_id", "ends_at_ms"], "RE WrapUpStarted"),
        "result": frame("result", {**seq, "id": UUID, "ok": {"type": "boolean"}, "error": obj(
            {"code": {"enum": ["bad_request", "not_found", "not_your_call", "invalid_state", "offer_gone",
                               "call_unmanaged", "unavailable", "destination_not_allowed", "rate_limited",
                               "conflict", "invalid_transition"]},
             "retry": {"type": "boolean"}}, ["code", "retry"])},
            ["seq", "id", "ok"], "The answer to a command"),
        "ice_servers": frame("ice_servers", {**seq, "expires_at_ms": MS, "ice_servers": {"type": "array", "items": obj(
            {"urls": {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": "^(stun|turn|turns):"}},
             "username": {"type": "string", "maxLength": 256}, "credential": secret}, ["urls"])}},
            ["seq", "ice_servers", "expires_at_ms"], "TURN REST credentials, renewed at half their lifetime"),
        "sip_credentials": frame("sip_credentials", {**seq, "uri": {"type": "string", "pattern": "^sip:"},
                                                     "ws_servers": {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": "^wss?://"}},
                                                     "token": secret, "expires_at_ms": MS},
                                 ["seq", "uri", "ws_servers", "token", "expires_at_ms"],
                                 "A short-lived SIP JWT bound to this session (opensips auth_jwt)"),
        "reconnect": frame("reconnect", {**seq, "after_ms": {"type": "integer", "minimum": 0, "maximum": 30000}},
                           ["seq", "after_ms"], "Drain or bin move: reconnect after after_ms"),
        "ping": frame("ping", seq, ["seq"], "Every 15 s; two unanswered close the socket"),
        "queues": frame("queues", {**seq, "queues": MEMBERSHIP, "pending_queues": MEMBERSHIP, "queues_version": MS,
                                   "reason": {"enum": ["login", "optimizer", "supervisor_pin", "supervisor_unpin", "config",
                                                       "mode_change", "fallback", "unspecified"]}},
                        ["seq", "queues", "queues_version", "reason"],
                        "0.11.0: RE QueuesChanged: the agent's queues now and those waiting for its next offer (never mid-call); "
                        "a weights-only change sends none"),
    }
    return schema("to-desktop", f, "Gateway -> desktop. No phone number and no digit in any frame (GW-010). A desktop "
                  "ignores a frame type it doesn't know (0.11.0, CR-015): new kinds are additive.")


def write(rel, doc):
    p = ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def ws(name, valid, direction, fr, desc, violates=()):
    d = {"description": desc, "kind": "ws", "writer_valid": valid, "direction": direction, "frame": fr}
    if not valid:
        d["violates"] = list(violates)
    write(f"fixtures/agent-ws.v1/{'valid' if valid else 'invalid'}/{name}.json", d)


def ws_fixtures():
    cid = "0192f6a1-c000-7aaa-8bbb-000000000001"
    g, d = "to_gateway", "to_desktop"
    agent = {"state": "ready", "since_ms": T0, "state_version": 4, "mode": "inbound"}
    call = {"interaction_id": INTERACTION, "reservation_id": RES, "state": "connected", "held": False, "muted": False,
            "consulting": False, "recording": "on", "managed": True,
            "parties": [{"role": "customer", "leg_id": LEG_A, "live": True},
                        {"role": "agent", "agent_id": AGENT, "leg_id": LEG_B, "live": True}]}
    ws("auth", True, g, {"v": 1, "type": "auth", "token": "eyJhbGciOiJSUzI1NiJ9.test.signature", "client_version": "0.1.0"}, "The first frame")
    ws("set-state-not-ready", True, g, {"v": 1, "type": "set_state", "id": cid, "state": "not_ready", "reason_code": "break"}, "Not ready with a reason")
    ws("accept", True, g, {"v": 1, "type": "accept", "id": cid, "offer_id": OFFER}, "Accept an offer")
    ws("hold", True, g, {"v": 1, "type": "hold", "id": cid, "interaction_id": INTERACTION}, "Hold through IE")
    ws("transfer-number", True, g, {"v": 1, "type": "transfer", "id": cid, "interaction_id": INTERACTION,
                                    "target": {"number": "+447700900123"}}, "A blind transfer to a number (PII: phone, desktop to gateway only)")
    ws("wrap-up-done", True, g, {"v": 1, "type": "wrap_up_done", "id": cid, "reservation_id": RES, "disposition_code": "sale"}, "End wrap-up")
    ws("snapshot", True, d, {"v": 1, "type": "snapshot", "seq": 1, "session_id": SESSION, "agent": agent,
                             "reservations": [{"reservation_id": RES, "interaction_id": INTERACTION, "origin": "inbound"}],
                             "calls": [call], "wrap_ups": []}, "After a reconnect mid-call")
    ws("offer", True, d, {"v": 1, "type": "offer", "seq": 2, "offer_id": OFFER, "interaction_id": INTERACTION,
                          "channel": "voice", "queue_id": "sales", "expires_at_ms": T0 + 15000, "consult": False, "direct": False}, "A voice offer")
    ws("reserved", True, d, {"v": 1, "type": "reserved", "seq": 3, "reservation_id": RES, "interaction_id": INTERACTION, "origin": "inbound"}, "Auto-answer allowed")
    ws("call-unmanaged", True, d, {"v": 1, "type": "call", "seq": 4, "call": {**call, "managed": False}}, "IE lost control: the call stays up")
    ws("result-not-your-call", True, d, {"v": 1, "type": "result", "seq": 5, "id": cid, "ok": False,
                                         "error": {"code": "not_your_call", "retry": False}}, "A refused control")
    ws("sip-credentials", True, d, {"v": 1, "type": "sip_credentials", "seq": 6, "uri": CONTACT,
                                    "ws_servers": ["wss://sip.acme.eu1.platform.example"], "token": "eyJhbGciOiJIUzI1NiJ9.sip.sig",
                                    "expires_at_ms": T0 + 300000}, "Session-bound SIP token")
    ws("reconnect", True, d, {"v": 1, "type": "reconnect", "seq": 7, "after_ms": 4210}, "Drain")
    ws("queues", True, d, {"v": 1, "type": "queues", "seq": 8,
                           "queues": [{"queue_id": "billing", "tier": "primary", "since_ms": T0},
                                      {"queue_id": "sales", "tier": "secondary", "since_ms": T0},
                                      {"queue_id": "support", "tier": "secondary", "pinned": True, "since_ms": T0}],
                           "pending_queues": [{"queue_id": "support", "tier": "primary"}], "queues_version": 3,
                           "reason": "optimizer"}, "0.11.0: three queues now; moving to support after this call")
    ws("offer-secondary", True, d, {"v": 1, "type": "offer", "seq": 9, "offer_id": OFFER, "interaction_id": INTERACTION,
                                    "channel": "voice", "queue_id": "sales", "expires_at_ms": T0 + 15000,
                                    "tier": "secondary"}, "0.11.0: an offer from a backup queue")
    ws("auth-without-token", False, g, {"v": 1, "type": "auth"}, "No token", ["schema"])
    ws("hold-without-id", False, g, {"v": 1, "type": "hold", "interaction_id": INTERACTION}, "Commands carry an id", ["schema"])
    ws("set-state-names-tenant", False, g, {"v": 1, "type": "set_state", "id": cid, "state": "ready", "tenant_id": OTHER},
       "The tenant never comes from a frame", ["schema"])
    ws("id-not-uuid", False, g, {"v": 1, "type": "mute", "id": "../x", "interaction_id": INTERACTION}, "id is a UUID", ["schema"])
    ws("offer-with-number", False, d, {"v": 1, "type": "offer", "seq": 2, "offer_id": OFFER, "interaction_id": INTERACTION,
                                       "expires_at_ms": T0, "caller_number": "+447700900123"}, "No number reaches the desktop", ["schema"])


# ---- gRPC scripts ----------------------------------------------------------------------------

def call(pkg, svc, rpc, body, expect, tenant=TENANT, note=None):
    meta = {"traceparent": TRACE}
    if tenant:
        meta["tenant-id"] = tenant
    s = {"op": "call", "method": f"{pkg}.{svc}/{rpc}", "message": f"{pkg}.{rpc}Request", "body": body,
         "metadata": meta, "expect": expect}
    if note:
        s["note"] = note
    return s


def ok(pkg, rpc, body):
    return {"message": f"{pkg}.{rpc}Response", "body": body}


def err(status, code):
    return {"status": status, "parity_error": code}


def script(pkg, name, desc, steps, given):
    write(f"fixtures/{pkg.split('.')[1]}.v1/valid/{name}.json",
          {"description": desc, "kind": "grpc", "writer_valid": True, "package": pkg, "given": given, "steps": steps})


def agent_given(state="NOT_READY", reason="login", on_call=False):
    g = {"start_ms": str(T0), "leader_epoch": "7", "offer_timeout_ms": "15000", "grace_ms": "60000",
         "agents": [{"tenant_id": TENANT, "agent_id": AGENT, "state": state}]}
    if reason:
        g["agents"][0]["reason_code"] = reason
    if on_call:
        g["agents"][0]["reservation"] = {"reservation_id": RES, "interaction_id": INTERACTION}
    return g


def queue_call():
    return {"op": "inject", "what": "queue_call",
            "args": {"tenant_id": TENANT, "interaction_id": INTERACTION, "queue_id": "sales"}}


def call_given(managed=True, blocked=(), owner_moving=False):
    return {"calls": [{"tenant_id": TENANT, "interaction_id": INTERACTION, "reservation_id": RES, "agent_id": AGENT,
                       "customer_leg_id": LEG_A, "agent_leg_id": LEG_B, "managed": managed, "owner_epoch": "1042",
                       "seq": "12"}],
            "blocked_numbers": list(blocked), "owner_moving": owner_moving}


A = "parity.agents.v1"
I = "parity.interaction.v1"


def ag(rpc, body, expect, tenant=TENANT, note=None):
    return call(A, "AgentService", rpc, body, expect, tenant, note)


def sess(gen=1, s=SESSION):
    return {"tenant_id": TENANT, "agent_id": AGENT, "session_id": s, "attach_generation": str(gen)}


def view(state, version, reason=None, pending=None):
    v = {"state": f"AGENT_STATE_{state}", "since_ms": str(T0), "state_version": str(version), "mode": "MODE_INBOUND"}
    if reason:
        v["reason_code"] = reason
    if pending:
        v["pending_target"] = f"TARGET_STATE_{pending}"
    return v


def deliver(seq, gen=1, **event):
    body = {"leader_epoch": "7", "tenant_id": TENANT, "agent_id": AGENT, "attach_generation": str(gen),
            "delivery_seq": str(seq), **event}
    return {"op": "deliver", "message": f"{A}.WatchResponse", "body": body}


def watch():
    return ag("Watch", {"replica_id": REPLICA, "bin": BIN},
              ok(A, "Watch", {"leader_epoch": "7", "delivery_seq": "0", "heartbeat": {"at_ms": str(T0)}}), tenant=None)


def attach(session=SESSION, gen=1, existing=False, state=view("NOT_READY", 1, "login"), reservations=None):
    body = {"attach_generation": str(gen), "existing": existing, "agent": state, "leader_epoch": "7"}
    if reservations:
        body["reservations"] = reservations
    return ag("AttachAgent", {"tenant_id": TENANT, "agent_id": AGENT, "replica_id": REPLICA, "session_id": session,
                              "agent_contact": CONTACT}, ok(A, "AttachAgent", body))


def cid(n, agent=AGENT, tenant=TENANT):
    return command_id(tenant, agent, f"0192f6a1-c000-7aaa-8bbb-{n:012d}")


def agents_scripts():
    offer = {"offer_id": OFFER, "interaction_id": INTERACTION, "channel": "CHANNEL_VOICE", "queue_id": "sales",
             "expires_at_ms": str(T0 + 15000)}
    res = {"reservation_id": RES, "interaction_id": INTERACTION, "origin": "RESERVATION_ORIGIN_INBOUND",
           "deadline_ms": str(T0 + 45000)}
    script(A, "attach-offer-accept", "Login, ready, an offer accepted: Reserved names the interaction to auto-answer", [
        watch(), attach(),
        ag("SetState", {"session": sess(), "command_id": cid(1), "target": "TARGET_STATE_READY"},
           ok(A, "SetState", {"agent": view("READY", 2)})),
        queue_call(), deliver(1, offer=offer),
        ag("AnswerOffer", {"session": sess(), "command_id": cid(2), "offer_id": OFFER, "decision": "DECISION_ACCEPT"},
           ok(A, "AnswerOffer", {"decision": "DECISION_ACCEPT", "reservation": res})),
        deliver(2, reserved=res),
        deliver(3, state_changed={"agent": view("RESERVED", 4)}),
        ag("Acknowledge", {"replica_id": REPLICA, "acks": [
            {"tenant_id": TENANT, "agent_id": AGENT, "attach_generation": "1", "delivery_seq": str(n)} for n in (1, 2, 3)]},
           ok(A, "Acknowledge", {}), tenant=None),
    ], agent_given())
    script(A, "offer-expired", "An unanswered offer: revoked, the agent not ready (missed_offer); a late accept: offer_gone", [
        watch(), attach(state=view("READY", 2)),
        queue_call(), deliver(1, offer=offer),
        {"op": "advance", "ms": "15000"},
        deliver(2, offer_revoked={"offer_id": OFFER, "reason": "REVOKE_REASON_EXPIRED"}),
        deliver(3, state_changed={"agent": view("NOT_READY", 4, "missed_offer")}),
        ag("AnswerOffer", {"session": sess(), "command_id": cid(3), "offer_id": OFFER, "decision": "DECISION_ACCEPT"},
           err("FAILED_PRECONDITION", "offer_gone")),
    ], agent_given("READY", None))
    script(A, "wrap-up-pending-not-ready", "Not ready asked on a call is pending; wrap-up ends into it", [
        watch(), attach(state=view("ON_CALL", 5), reservations=[{"reservation_id": RES, "interaction_id": INTERACTION}]),
        ag("SetState", {"session": sess(), "command_id": cid(4), "target": "TARGET_STATE_NOT_READY", "reason_code": "break"},
           ok(A, "SetState", {"agent": {**view("ON_CALL", 5, pending="NOT_READY"), "pending_reason_code": "break"}})),
        {"op": "inject", "what": "call_ended", "args": {"tenant_id": TENANT, "reservation_id": RES}},
        deliver(1, wrap_up_started={"reservation_id": RES, "interaction_id": INTERACTION, "ends_at_ms": str(T0 + 60000)}),
        ag("EndWrapUp", {"session": sess(), "command_id": cid(5), "reservation_id": RES, "disposition_code": "sale"},
           ok(A, "EndWrapUp", {"agent": view("NOT_READY", 7, "break")})),
    ], agent_given("ON_CALL", None, on_call=True))
    script(A, "superseded-session", "A second tab attaches: the first is detached and fenced", [
        watch(), attach(),
        attach(session=SESSION2, gen=2),
        deliver(1, gen=1, detached={"reason": "DETACHED_REASON_SUPERSEDED"}),
        ag("SetState", {"session": sess(1), "command_id": cid(6), "target": "TARGET_STATE_READY"},
           err("ABORTED", "session_superseded")),
        ag("DetachAgent", {"session": sess(1), "reason": "DETACH_REASON_SOCKET_CLOSED"},
           ok(A, "DetachAgent", {"applied": False}), note="the old replica's late detach changes nothing"),
        attach(session=SESSION2, gen=2, existing=True),
    ], agent_given())
    script(A, "detach-revokes-offer", "A lost socket: the pending offer goes elsewhere, no penalty; no more offers", [
        watch(), attach(state=view("READY", 2)),
        queue_call(), deliver(1, offer=offer),
        ag("DetachAgent", {"session": sess(), "reason": "DETACH_REASON_SOCKET_CLOSED"}, ok(A, "DetachAgent", {"applied": True})),
        deliver(2, offer_revoked={"offer_id": OFFER, "reason": "REVOKE_REASON_DETACHED"}),
    ], agent_given("READY", None))
    script(A, "command-id-conflict", "The same command_id with another request: conflict", [
        watch(), attach(),
        ag("SetState", {"session": sess(), "command_id": cid(7), "target": "TARGET_STATE_READY"},
           ok(A, "SetState", {"agent": view("READY", 2)})),
        ag("SetState", {"session": sess(), "command_id": cid(7), "target": "TARGET_STATE_READY"},
           ok(A, "SetState", {"agent": view("READY", 2)}), note="a repeat: the first result"),
        ag("SetState", {"session": sess(), "command_id": cid(7), "target": "TARGET_STATE_LOGGED_OUT"},
           err("ALREADY_EXISTS", "conflict")),
    ], agent_given())
    script(A, "tenant-mismatch", "The metadata's tenant differs from the request's", [
        watch(),
        ag("AttachAgent", {"tenant_id": TENANT, "agent_id": AGENT, "replica_id": REPLICA, "session_id": SESSION,
                           "agent_contact": CONTACT}, err("INVALID_ARGUMENT", "tenant_mismatch"), tenant=OTHER),
    ], agent_given())


def ie(rpc, body, expect, tenant=TENANT, note=None):
    return call(I, "InteractionControlService", rpc, body, expect, tenant, note)


def view_call(held=False, muted=False, consulting=False, parties=None, seq=12):
    return {"interaction_id": INTERACTION, "state": "CALL_STATE_CONNECTED", "held": held, "muted": muted,
            "consulting": consulting, "parties": parties or [
                {"role": "PARTY_ROLE_CUSTOMER", "leg_id": LEG_A, "live": True},
                {"role": "PARTY_ROLE_AGENT", "agent_id": AGENT, "leg_id": LEG_B, "live": True}],
            "recording": "RECORDING_STATE_ON", "managed": True, "owner_epoch": "1042", "seq": str(seq),
            "reservation_id": RES}


def ex(n, action, agent=AGENT):
    return {"tenant_id": TENANT, "interaction_id": INTERACTION, "agent_id": agent, "reservation_id": RES,
            "command_id": cid(n, agent), **action}


def control_scripts():
    script(I, "hold-resume", "Get, hold, a repeat of the same command_id, resume", [
        ie("Get", {"tenant_id": TENANT, "interaction_id": INTERACTION, "agent_id": AGENT}, ok(I, "Get", {"call": view_call()})),
        ie("Execute", ex(10, {"hold": {}}), ok(I, "Execute", {"outcome": "OUTCOME_DONE", "call": view_call(held=True, seq=13)})),
        ie("Execute", ex(10, {"hold": {}}), ok(I, "Execute", {"outcome": "OUTCOME_ALREADY_DONE", "call": view_call(held=True, seq=13)})),
        ie("Execute", ex(11, {"resume": {}}), ok(I, "Execute", {"outcome": "OUTCOME_DONE", "call": view_call(seq=14)})),
        ie("Execute", ex(12, {"resume": {}}), err("FAILED_PRECONDITION", "invalid_state"), note="not held"),
    ], call_given())
    gone = [{"role": "PARTY_ROLE_CUSTOMER", "leg_id": LEG_A, "live": True},
            {"role": "PARTY_ROLE_AGENT", "agent_id": AGENT, "leg_id": LEG_B, "live": False}]
    script(I, "transfer-then-hangup-refused", "After a blind transfer the old agent holds neither the reservation nor a live leg", [
        ie("Execute", ex(13, {"blind_transfer": {"target": {"queue_id": "billing"}}}),
           ok(I, "Execute", {"outcome": "OUTCOME_DONE", "call": {**view_call(parties=gone, seq=15), "reservation_id": ""}})),
        ie("Execute", ex(14, {"hangup": {}}), err("PERMISSION_DENIED", "not_your_call")),
        ie("Get", {"tenant_id": TENANT, "interaction_id": INTERACTION, "agent_id": AGENT}, err("PERMISSION_DENIED", "not_your_call")),
    ], call_given())
    consult = [{"role": "PARTY_ROLE_CUSTOMER", "leg_id": LEG_A, "live": True},
               {"role": "PARTY_ROLE_AGENT", "agent_id": AGENT, "leg_id": LEG_B, "live": True},
               {"role": "PARTY_ROLE_AGENT", "agent_id": AGENT_B, "leg_id": LEG_C, "live": True}]
    script(I, "consult-live-leg", "A consulted agent with a live leg may act; an uninvolved agent may not", [
        ie("Execute", ex(15, {"mute": {}}, AGENT_B), err("PERMISSION_DENIED", "not_your_call"), note="not in the call yet"),
        ie("Execute", ex(16, {"consult": {"target": {"agent_id": AGENT_B}}}),
           ok(I, "Execute", {"outcome": "OUTCOME_DONE", "call": view_call(held=True, consulting=True, parties=consult, seq=16)})),
        ie("Execute", ex(17, {"mute": {}}, AGENT_B), ok(I, "Execute", {"outcome": "OUTCOME_DONE", "call": {
            **view_call(held=True, muted=True, consulting=True, parties=consult, seq=17)}})),
        ie("Execute", ex(18, {"merge": {}}), ok(I, "Execute", {"outcome": "OUTCOME_DONE", "call": view_call(parties=consult, seq=18)})),
    ], call_given())
    script(I, "command-id-conflict", "The same command_id with another action: conflict", [
        ie("Execute", ex(19, {"mute": {}}), ok(I, "Execute", {"outcome": "OUTCOME_DONE", "call": view_call(muted=True, seq=13)})),
        ie("Execute", ex(19, {"unmute": {}}), err("ALREADY_EXISTS", "conflict")),
    ], call_given())
    script(I, "unmanaged", "IE lost the node's TC (T14): controls are refused, the call stays up", [
        ie("Execute", ex(20, {"hold": {}}), err("FAILED_PRECONDITION", "call_unmanaged")),
        ie("Get", {"tenant_id": TENANT, "interaction_id": INTERACTION, "agent_id": AGENT},
           ok(I, "Get", {"call": {**view_call(), "managed": False}})),
    ], call_given(managed=False))
    script(I, "refusals", "A blocked destination, an unknown interaction, a tenant mismatch", [
        ie("Execute", ex(21, {"blind_transfer": {"target": {"number": "+447700900999"}}}),
           err("FAILED_PRECONDITION", "destination_not_allowed")),
        ie("Execute", {**ex(22, {"hangup": {}}), "interaction_id": "0192f6a1-7c3e-7b21-9a4d-000000000000"}, err("NOT_FOUND", "not_found")),
        ie("Execute", ex(23, {"hangup": {}}), err("INVALID_ARGUMENT", "tenant_mismatch"), tenant=OTHER),
    ], call_given(blocked=["+447700900999"]))
    script(I, "owner-moving", "An adoption gap: retry with the same command_id", [
        ie("Execute", ex(24, {"hangup": {}}), err("UNAVAILABLE", "owner_moving")),
    ], call_given(owner_moving=True))

if __name__ == "__main__":
    for d in ["fixtures/agent-ws.v1", "fixtures/agents.v1", "fixtures/interaction.v1"]:
        shutil.rmtree(ROOT / d, ignore_errors=True)
    write("schemas/agent-ws/to-gateway.v1.json", to_gateway())
    write("schemas/agent-ws/to-desktop.v1.json", to_desktop())
    ws_fixtures()
    agents_scripts()
    control_scripts()
    # 0.11.0: parity-cc-routing's agents.v1 scripts (membership, pins, moves, forced states) live in
    # the same directory: write them again after the rmtree.
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import membership_scripts  # noqa: E402

    membership_scripts.main()
    print("ok")
