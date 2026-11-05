#!/usr/bin/env python3
"""Writes the log-record.v1 catalogues and fixtures (0.13.0; CONVENTIONS "Logging (log-record.v1)"):
- logging/events.common.v1.json: the events every service shares (normative);
- logging/examples/events.<service>.v1.json: starter catalogues, one per service. Each service copies its
  file to its own repository as logging/events.<service>.v1.json and owns it from then on;
- fixtures/log-record.v1/: valid samples per service family and the cross-service call (kind "log"),
  and invalid records, each breaking the rules its `violates` names.
The schemas (schemas/logging/*.json) are written by hand. Run from anywhere:
python3 -I scripts/logging_fixtures.py. Output is deterministic.
"""
import json
import pathlib
import shutil

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures" / "log-record.v1"

# --- identities and ids -----------------------------------------------------------------------
TENANT = "acme"
XSID = "0192f3c1-6a2b-7c3d-8e4f-5a6b7c8d9e01"          # the customer's call (UUIDv7)
CONSULT_XSID = "0192f3c1-7b10-7a01-9b02-0c0d0e0f1a2b"  # agent-7 consults agent-12: a child call
XSID_V4 = "0192f3c1-6a2b-4c3d-8e4f-5a6b7c8d9e01"       # what opensips' $uuid makes today (not v7)
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
LEG_CUSTOMER = "6f1c2a9e-0000-4000-8000-000000000001"
LEG_AGENT = "6f1c2a9e-0000-4000-8000-000000000002"
LEG_CONSULT = "6f1c2a9e-0000-4000-8000-000000000003"
ENTRY = "0192f3c1-6b00-7000-8000-00000000e001"
RESERVATION = "0192f3c1-6c00-7000-8000-00000000a001"
CONSULT_ENTRY = "0192f3c1-7c00-7000-8000-00000000e002"
CONSULT_RESERVATION = "0192f3c1-7d00-7000-8000-00000000a002"
EVENT_ID = "0192f3c1-6e00-7000-8000-0000000ee001"
IE_CMD = "ie-0:0192f3c1-6f00-7000-8000-00000000c001"

SERVICES = {
    "ie": ("parity-interaction-engine", "0.6.0+a1b2c3d", "ie-b0-0", None),
    "tc": ("parity-telephony-controller", "0.4.1", "tc-media-1", "tc-7f3a"),
    "re": ("parity-cc-routing", "0.5.0", "re-b0-1", None),
    "gw": ("parity-gateway", "0.3.2", "gw-0", None),
    "ms": ("parity-ms", "0.9.0", "ms-media-1", "ms-boot-19"),
    "st": ("parity-store", "0.2.0", "store-0", None),
    "bl": ("parity-billing", "0.1.3", "billing-0", None),
    "fe": ("parity-frontend", "0.1.4", "frontend-0", None),
    "ac": ("parity-admin-console", "0.2.0", "console-0", None),
    "rs": ("parity-rs", "0.2.0", "rs-eu1a-0", None),
    "ct": ("parity-cc-tools", "0.1.0", "cct-0", None),
}
OP_ID = "0193a1b2-c3d4-7e5f-8a6b-7c8d9e0f1a2b"      # one confirmed operator action (0.16.0)
SAGA_ID = "0193a1b2-c3d4-7e5f-9a6b-7c8d9e0f1a2c"
ENVELOPE_ORDER = ["ts", "level", "service", "version", "instance", "incarnation", "seq", "event", "msg"]


class Clock:
    """Hands out ts values (ms after 10:00:00.000Z) and per-instance seq numbers."""

    def __init__(self):
        self.ms = 0
        self.seq = {}

    def stamp(self, step_ms):
        self.ms += step_ms
        s, ms = divmod(self.ms, 1000)
        m, s = divmod(s, 60)
        return f"2026-11-01T10:{m:02d}:{s:02d}.{ms:03d}Z"

    def next_seq(self, who):
        self.seq[who] = self.seq.get(who, 100) + 1
        return self.seq[who]


def rec(clock, who, event, msg, level="info", step=3, **fields):
    service, version, instance, incarnation = SERVICES[who]
    r = {"ts": clock.stamp(step), "level": level, "service": service, "version": version,
         "instance": instance}
    if incarnation:
        r["incarnation"] = incarnation
    r["seq"] = clock.next_seq(who)
    r["event"] = event
    r["msg"] = msg
    r.update(fields)
    return r


def call(xsid=XSID, **more):
    """The correlation every call-scoped record of the customer's call carries."""
    return {"tenant_id": TENANT, "xsid": xsid, "trace_id": TRACE, **more}


# --- catalogues -------------------------------------------------------------------------------
def ev(description, levels=("info",), call_scoped=False, requires=(), attrs=(), transition=False,
       decision=False, sample_rate=None, dedup=None):
    e = {"description": description, "levels": list(levels)}
    if call_scoped:
        e["call_scoped"] = True
    if requires:
        e["requires"] = list(requires)
    if attrs:
        e["attrs"] = list(attrs)
    if transition:
        e["transition"] = True
    if decision:
        e["decision"] = True
    if sample_rate is not None:
        e["sample_rate"] = sample_rate
    if dedup:
        e["dedup"] = dedup
    return e


COMMON = {
    "service.starting": ev("The process started and read its settings; nothing is served yet."),
    "service.ready": ev("Every readiness check passed: the healthy file is written, traffic may come."),
    "service.not_ready": ev("A readiness check fails; attrs.failing names the checks.", ("warn",),
                            attrs=("failing",)),
    "service.draining": ev("Draining began (cause: the signal or operator action): no new work is taken."),
    "service.stopped": ev("The process is exiting with attrs.exit_code.", ("info", "error"), attrs=("exit_code",)),
    "service.panic": ev("An unexpected crash (a Rust panic, an uncaught Java error): error.stack holds the stack.",
                        ("fatal",), requires=("error",)),
    "config.loaded": ev("A configuration file or document was read and accepted (attrs.file).", attrs=("file",)),
    "config.refused": ev("A configuration file or document was refused; the old one stays (attrs.file).",
                         ("error",), requires=("error",), attrs=("file",)),
    "log.dropped": ev("Records were dropped because the log queue was full (attrs.dropped since the last report)."
                      " The seq gap shows where.", ("warn",), attrs=("dropped",)),
    "dependency.unavailable": ev("A dependency (attrs.dependency: kafka, etcd, postgres, redis, routing,"
                                 " telephony) can't be reached; the service retries.", ("warn", "error"),
                                 requires=("error",), attrs=("dependency",),
                                 dedup={"window_ms": 10000, "key": ["attrs.dependency"]}),
    "dependency.restored": ev("The dependency in attrs.dependency answers again.", attrs=("dependency",)),
}

CATALOGUES = {
    "parity-interaction-engine": {
        "interaction.created": ev("A call became an interaction: IE owns it from here (attrs.origin: pstn, agent,"
                                  " api, callback; parent_xsid when another call caused it).", call_scoped=True,
                                  requires=("bin", "owner_epoch"), attrs=("origin",)),
        "interaction.state_changed": ev("The interaction's state machine moved (new, ivr, queued, offered,"
                                        " connected, consulting, wrap_up, ended).", ("info",), call_scoped=True,
                                        transition=True),
        "interaction.adopted": ev("This replica took over a call whose owner died (owner_epoch is the new one).",
                                  ("warn",), call_scoped=True, requires=("owner_epoch",)),
        "interaction.ended": ev("The customer's call ended (attrs.end_reason: completed, abandoned, failed,"
                                " busy, no_answer).", call_scoped=True, attrs=("end_reason",)),
        "leg.added": ev("A leg joined the call (attrs.role: customer, agent, consult, supervisor).",
                        call_scoped=True, requires=("leg_id",), attrs=("role",)),
        "leg.removed": ev("A leg left the call.", call_scoped=True, requires=("leg_id",), attrs=("reason",)),
        "ivr.input_collected": ev("The IVR collected input: attrs.presses counts keys. Digits never appear.",
                                  call_scoped=True, attrs=("presses", "step")),
        "routing.enqueued": ev("Enqueue to the routing engine answered.", call_scoped=True,
                               requires=("queue_id", "entry_id", "op", "outcome")),
        "routing.assigned": ev("The routing engine assigned an agent (Watch: Assigned).", call_scoped=True,
                               requires=("agent_id", "reservation_id")),
        "routing.rpc_failed": ev("A routing RPC failed; outcome retry while attempts remain.", ("warn", "error"),
                                 call_scoped=True, requires=("op", "attempt", "outcome", "error")),
        "command.applied": ev("An interaction.commands command was applied.", call_scoped=True,
                              requires=("command_id", "op", "outcome")),
        "command.refused": ev("An interaction.commands command was answered with an error code.",
                              ("info", "warn"), call_scoped=True, requires=("command_id", "op", "error")),
        "consult.started": ev("An agent started a consult: a child call with its own xsid, parent_xsid the"
                              " customer's call.", call_scoped=True, requires=("parent_xsid", "agent_id")),
        "consult.completed": ev("The consult completed (warm transfer): the consulted agent's leg joins the"
                                " parent call, which keeps its xsid.", call_scoped=True,
                                requires=("parent_xsid", "leg_id")),
        "cdr.written": ev("The CDR row for the interaction was written (insert-only, idempotent).",
                          call_scoped=True, requires=("outcome",)),
        "owner.lease_lost": ev("No etcd keepalive worked for a whole TTL: new calls are declined.", ("error",),
                               requires=("bin",)),
    },
    "parity-telephony-controller": {
        "session.arrived": ev("A call arrived from the edge; the xsid is read from X-Parity-Interaction-Id"
                              " (X-Interaction-ID until opensips moves).", call_scoped=True,
                              requires=("node", "leg_id"), attrs=("direction",)),
        "leg.ringing": ev("A leg TC dialled is ringing.", call_scoped=True, requires=("leg_id",)),
        "leg.answered": ev("A leg answered (attrs.role).", call_scoped=True, requires=("leg_id",),
                           attrs=("role",)),
        "leg.state_changed": ev("A leg's state machine moved.", ("debug",), call_scoped=True,
                                requires=("leg_id",), transition=True),
        "leg.ended": ev("A leg ended (attrs.sip_status).", call_scoped=True, requires=("leg_id",),
                        attrs=("sip_status",)),
        "command.executed": ev("A telephony.v1 command finished.", ("info", "debug"), call_scoped=True,
                               requires=("command_id", "op", "outcome", "duration_ms")),
        "command.failed": ev("A telephony.v1 command failed.", ("warn", "error"), call_scoped=True,
                             requires=("command_id", "op", "error")),
        "dtmf.received": ev("A key press on a leg. The digit never appears (a masker writes [redacted]).",
                            ("debug",), call_scoped=True, requires=("leg_id",)),
        "recording.started": ev("Recording started on a leg (attrs.recording_id).", call_scoped=True,
                                requires=("leg_id",), attrs=("recording_id",)),
        "node.lost": ev("A media node stopped answering.", ("error",), requires=("node",)),
    },
    "parity-cc-routing": {
        "entry.enqueued": ev("A caller joined a queue.", call_scoped=True,
                             requires=("queue_id", "entry_id", "bin", "leader_epoch")),
        "entry.dequeued": ev("A caller left a queue without an agent (attrs.reason).", call_scoped=True,
                             requires=("queue_id", "entry_id"), attrs=("reason",)),
        "agent.selected": ev("An agent was chosen for a caller. decision: the candidates and score parts"
                             " (debug, or info sampled at sample_rate).", ("debug", "info"), call_scoped=True,
                             requires=("queue_id", "entry_id", "agent_id", "reservation_id"), decision=True,
                             sample_rate=0.01),
        "offer.sent": ev("An offer went to the agent's gateway session.", call_scoped=True,
                         requires=("agent_id", "reservation_id"), attrs=("timeout_ms",)),
        "offer.accepted": ev("The agent accepted the offer.", call_scoped=True,
                             requires=("agent_id", "reservation_id")),
        "offer.expired": ev("The offer timed out; the caller keeps its place.", call_scoped=True,
                            requires=("agent_id", "reservation_id")),
        "agent.state_changed": ev("An agent's routing state moved.", requires=("agent_id",), transition=True),
        "weights.updated": ev("The optimizer moved a queue's weight (lever a); decision says why.",
                              ("info", "debug"), requires=("queue_id",), decision=True, sample_rate=1),
        "request.refused": ev("An RPC was refused (error.code); rate limited per op (CR-069).", ("warn",),
                              requires=("op", "error"), dedup={"window_ms": 1000, "key": ["op"]}),
        "leader.elected": ev("This replica leads its bin (leader_epoch).", requires=("bin", "leader_epoch")),
    },
    "parity-gateway": {
        "session.opened": ev("An agent desktop's WebSocket session opened.", requires=("agent_id", "session_id")),
        "session.closed": ev("A desktop session closed (attrs.close_code).", requires=("agent_id", "session_id"),
                             attrs=("close_code",)),
        "offer.delivered": ev("An offer frame was written to the agent's desktop.", call_scoped=True,
                              requires=("agent_id", "reservation_id", "session_id")),
        "offer.answered": ev("The desktop answered an offer (attrs.answer: accept, reject).", call_scoped=True,
                             requires=("agent_id", "reservation_id"), attrs=("answer",)),
        "frame.refused": ev("A desktop frame was refused (error.code).", ("warn",),
                            requires=("session_id", "error"), dedup={"window_ms": 5000, "key": ["session_id"]}),
        "routing.stream_lost": ev("The Watch stream to the routing engine broke; reconnecting.", ("warn",),
                                  requires=("bin",)),
    },
    "parity-ms": {
        "leg.started": ev("Media for a leg started (attrs.codec, attrs.mode).", call_scoped=True,
                          requires=("leg_id", "node"), attrs=("codec", "mode")),
        "leg.stats": ev("One summary per leg when it ends: packets, loss, jitter. Never per packet.",
                        call_scoped=True, requires=("leg_id",), attrs=("packets_in", "packets_out", "loss_pct")),
        "leg.media_timeout": ev("No RTP for the leg's media timeout.", ("warn",), call_scoped=True,
                                requires=("leg_id",)),
        "thread.overrun": ev("A media thread overran its cycle.", ("warn",), attrs=("thread_name",),
                             dedup={"window_ms": 1000, "key": ["attrs.thread_name"]}),
    },
    "parity-store": {
        "request.applied": ev("A tenant.store request was applied.", requires=("tenant_id", "request_id", "op",
                                                                            "outcome")),
        "request.refused": ev("A tenant.store request was refused (error.code).", ("info", "warn"),
                              requires=("request_id", "op", "error")),
        "outbox.published": ev("An outbox event was published.", ("debug",), requires=("event_id",)),
        "maps.loaded": ev("The tenant maps were loaded (attrs.revision).", attrs=("revision",)),
    },
    "parity-billing": {
        "webhook.received": ev("A Stripe webhook was verified and stored (attrs.stripe_event_type).",
                               requires=("request_id",), attrs=("stripe_event_type",)),
        "signup.state_changed": ev("A signup's saga moved.", requires=("request_id",), attrs=("signup_id",),
                                   transition=True),
        "entitlement.published": ev("A tenant's entitlements were published.", requires=("tenant_id", "event_id")),
        "payment.failed": ev("A payment failed (error.code).", ("warn",), requires=("tenant_id", "error"),
                             attrs=("billing_account_id",)),
    },
    "parity-frontend": {
        "http.request": ev("One HTTP request served (attrs.status).", ("info", "warn"),
                           requires=("request_id", "op", "outcome", "duration_ms"), attrs=("status",)),
        "signup.submitted": ev("A signup form was accepted (the address never appears).", requires=("request_id",),
                               attrs=("signup_id",)),
        "turnstile.refused": ev("The Turnstile check failed.", ("warn",), requires=("request_id", "error")),
        "email.sent": ev("A transactional email was handed to the provider (attrs.email_kind).",
                         attrs=("email_kind",)),
    },
    "parity-rs": {
        "request.handled": ev("A tenant routing request was answered (one per request).",
                              requires=("request_id", "op", "outcome")),
        "request.refused": ev("A request was refused (error.code, attrs.rule).", ("info", "warn"),
                              requires=("request_id", "op", "error"),
                              dedup={"window_ms": 10000, "key": ["op"]}),
        "request.dead_lettered": ev("A request without a valid id or reply_to was dead-lettered (attrs.reason).",
                                    ("warn",), attrs=("reason",)),
        "saga.accepted": ev("A saga was accepted (attrs.kind).", requires=("saga_id", "op_id"), attrs=("kind",)),
        "saga.state_changed": ev("A saga moved (cause: the input that moved it).", requires=("saga_id", "op_id"),
                                 attrs=("kind",), transition=True),
        "saga.finished": ev("A saga ended (attrs.saga_outcome: done, aborted, cancelled, compensated, "
                            "rolled_back, stopped).", ("info", "warn"), requires=("saga_id", "op_id"),
                            attrs=("kind", "saga_outcome")),
        "saga.blocked": ev("A saga needs an operator (resolve_saga).", ("error",), requires=("saga_id", "op_id")),
        "placement.decided": ev("place_tenant chose a zone and unit (decision: the candidate zones).",
                                ("info", "debug"), requires=("tenant_id", "saga_id"), decision=True,
                                sample_rate=1.0),
        "placement.written": ev("parity-store accepted a placement write (attrs.version).",
                                requires=("tenant_id", "saga_id"), attrs=("version",)),
        "placement.write_retried": ev("A placement write was sent again with the same id.", ("warn",),
                                      requires=("tenant_id", "saga_id"), attrs=("attempt_id",)),
        "assignment.written": ev("A tenant's assignment was written in the zone's etcd (attrs.epoch).",
                                 requires=("tenant_id", "saga_id"), attrs=("unit", "epoch")),
        "assignment.seeded": ev("A missing assignment was written from the placement.", requires=("tenant_id",),
                                attrs=("unit",)),
        "assignment.write_failed": ev("An assignment write failed or lost its compare.", ("warn",),
                                      requires=("tenant_id", "error")),
        "handover.reported": ev("A per-tenant service reported a handover (attrs.reporter).",
                                requires=("tenant_id",), attrs=("reporter", "epoch")),
        "unit.changed": ev("A unit's role, release, readiness or health changed.", attrs=("unit", "role")),
        "unit.scaled": ev("A unit's workloads were scaled (attrs.replicas).", attrs=("unit", "replicas")),
        "rollout.auto_rollback": ev("A failing canary started an automatic rollback.", ("error",),
                                    requires=("saga_id", "op_id"), attrs=("unit",)),
        "leader.acquired": ev("This instance became the zone's leader.", requires=("zone", "leader_epoch")),
        "leader.lost": ev("This instance lost the zone's leadership.", ("warn",), requires=("zone", "leader_epoch")),
        "store_copy.ready": ev("The copy of tenant.store.state reached its end offsets (attrs.revision).",
                               attrs=("revision",)),
        "drift.detected": ev("An assignment names another unit than the placement, with no saga to explain it.",
                             ("warn",), requires=("tenant_id",), attrs=("store_unit", "assignment_unit")),
        "import.row_applied": ev("The import job wrote one tenant_route row.", requires=("tenant_id",)),
        "import.row_skipped": ev("The import job kept what parity-store has (attrs.reason).",
                                 requires=("tenant_id",), attrs=("reason",)),
        "import.row_failed": ev("The import job failed one row.", ("warn",), requires=("tenant_id", "error")),
    },
    "parity-cc-tools": {
        "http.request": ev("One HTTP request served (attrs.status).", ("info", "warn"),
                           requires=("request_id", "op", "outcome", "duration_ms"), attrs=("status",)),
        "auth.login_succeeded": ev("An operator signed in to the ops realm (attrs.actor_sub).",
                                   attrs=("actor_sub",)),
        "auth.login_refused": ev("A sign-in or token was refused (attrs.reason).", ("warn",), attrs=("reason",),
                                 dedup={"window_ms": 10000, "key": ["attrs.reason"]}),
        "auth.step_up_required": ev("A high-risk action needs a fresh login.", requires=("op_id",)),
        "action.requested": ev("An operator confirmed an action; its audit row is written before it is sent.",
                               requires=("op_id",), attrs=("action", "actor_sub")),
        "action.answered": ev("The backend answered an action (error.code when refused).", ("info", "warn"),
                              requires=("op_id", "outcome")),
        "action.not_confirmed": ev("No answer yet: the page follows the saga's events.", ("warn",),
                                   requires=("op_id",)),
        "action.outcome": ev("The action's saga ended (attrs.saga_outcome).", requires=("op_id", "saga_id"),
                             attrs=("saga_outcome",)),
        "audit.write_failed": ev("An audit row could not be written: nothing was sent.", ("error",),
                                 requires=("op_id", "error")),
        "service_op.state_changed": ev("A service-control saga moved.", requires=("op_id", "saga_id"),
                                       attrs=("service_name", "driver"), transition=True),
        "service_op.finished": ev("A service-control saga ended (attrs.saga_outcome).",
                                  requires=("op_id", "saga_id"), attrs=("service_name", "saga_outcome")),
        "health.probe_failed": ev("A health probe failed (attrs.service_name, error.kind).", ("warn",),
                                  attrs=("service_name",),
                                  dedup={"window_ms": 60000, "key": ["attrs.service_name"]}),
        "k8s.call_failed": ev("A Kubernetes API call failed.", ("warn",), requires=("error",),
                              dedup={"window_ms": 60000, "key": ["op"]}),
    },
    "parity-admin-console": {
        "http.request": ev("One HTTP request served (attrs.status).", ("info", "warn"),
                           requires=("request_id", "op", "outcome", "duration_ms"), attrs=("status",)),
        "user.login": ev("A tenant user signed in (attrs.user_id).", requires=("tenant_id", "session_id"),
                         attrs=("user_id",)),
        "provisioning.requested": ev("A provisioning request was sent.", requires=("tenant_id", "request_id")),
        "call.details_viewed": ev("A tenant user opened a call's details (support quotes the xsid).",
                                  call_scoped=True, requires=("session_id",), attrs=("user_id",)),
    },
}


def catalogue(service, events, description):
    return {"schema_version": 1, "service": service, "description": description, "events": events}


def cat_path(service):
    return f"logging/examples/events.{service}.v1.json"


# --- fixtures ---------------------------------------------------------------------------------
def fixture(description, writer_valid, catalogues, records=None, line_text=None, violates=()):
    f = {"description": description, "kind": "log", "writer_valid": writer_valid,
         "violates": sorted(violates), "catalogues": [cat_path(s) for s in catalogues]}
    if records is not None:
        f["records"] = records
    else:
        f["line_text"] = line_text
    return f


def call_flow():
    c = Clock()
    ie = {"bin": "b0", "zone": "z1", "owner_epoch": 41}
    return [
        rec(c, "tc", "session.arrived", "call arrived from the edge", step=0, component="parity.telephonycontroller.node.Arrivals",
            cause="sip:INVITE", span_id="00f067aa0ba902b7", node="media-1", leg_id=LEG_CUSTOMER,
            **call(), attrs={"direction": "inbound", "from": "***123", "sip_call_id": "***999"}),
        rec(c, "ie", "interaction.created", "interaction created", component="interaction_engine::calls::owner",
            cause="rpc:parity.telephony.v1.TelephonyService/Watch:Arrived", span_id="1a2b3c4d5e6f7081",
            **call(**ie), attrs={"origin": "pstn", "flow": "main-ivr"},
            code={"file": "crates/interaction-engine/src/calls/owner.rs", "line": 702,
                  "func": "interaction_engine::calls::owner::bind"}),
        rec(c, "ie", "interaction.state_changed", "state changed", component="ie_core::machine",
            cause="internal:flow_start", from_state="new", to_state="ivr", **call(**ie)),
        rec(c, "ie", "ivr.input_collected", "ivr input collected", step=4200, component="ie_core::ivr",
            cause="rpc:parity.telephony.v1.TelephonyService/Watch:DtmfReceived", **call(**ie),
            attrs={"presses": 1, "step": "menu"}),
        rec(c, "ie", "interaction.state_changed", "state changed", component="ie_core::machine",
            cause="internal:ivr_route", from_state="ivr", to_state="queued", queue_id="sales", **call(**ie)),
        rec(c, "ie", "routing.enqueued", "enqueued", component="ie_routing_client::client",
            op="parity.routing.v1.RoutingService/Enqueue", outcome="ok", duration_ms=2.4, attempt=1,
            queue_id="sales", entry_id=ENTRY, span_id="2b3c4d5e6f708192", **call(**ie)),
        rec(c, "re", "entry.enqueued", "caller queued", step=1, component="routing/queue",
            cause="rpc:parity.routing.v1.RoutingService/Enqueue", span_id="3c4d5e6f708192a3", queue_id="sales",
            entry_id=ENTRY, bin="b0", leader_epoch=3, **call(),
            code={"file": "src/routing/queue.cc", "line": 318, "func": "Queue::enqueue"}),
        rec(c, "re", "agent.selected", "agent selected", level="debug", step=1, component="routing/match",
            cause="event:" + ENTRY, queue_id="sales", entry_id=ENTRY, agent_id="agent-7",
            reservation_id=RESERVATION, bin="b0", leader_epoch=3, **call(),
            decision={"kind": "agent.select", "total": 3, "chosen": "agent-7", "reason": "best_score",
                      "candidates": [
                          {"id": "agent-7", "score": 0.91, "eligible": True,
                           "parts": {"skill_match": 1.0, "idle_s": 0.6, "proficiency": 0.8}},
                          {"id": "agent-3", "score": 0.74, "eligible": True,
                           "parts": {"skill_match": 1.0, "idle_s": 0.2, "proficiency": 0.9}, "reason": "lower_score"},
                          {"id": "agent-9", "eligible": False, "reason": "not_ready"}]}),
        rec(c, "re", "offer.sent", "offer sent", step=1, component="routing/offers", queue_id="sales",
            agent_id="agent-7", reservation_id=RESERVATION, bin="b0", leader_epoch=3, **call(),
            attrs={"timeout_ms": 15000}),
        rec(c, "gw", "offer.delivered", "offer delivered to the desktop", step=2,
            component="parity.gateway.agents.OfferRelay", thread="offer-relay-1",
            cause="rpc:parity.agents.v1.AgentService/Watch:Offer", agent_id="agent-7",
            reservation_id=RESERVATION, session_id="ws-5d2e9a", duration_ms=1.1, **call()),
        rec(c, "gw", "offer.answered", "desktop answered the offer", step=2100,
            component="parity.gateway.agents.OfferRelay", thread="ws-in-4", cause="ws:offer_accept",
            agent_id="agent-7", reservation_id=RESERVATION, session_id="ws-5d2e9a", **call(),
            attrs={"answer": "accept"}),
        rec(c, "re", "offer.accepted", "offer accepted", step=2, component="routing/offers",
            cause="rpc:parity.agents.v1.AgentService/Accept", agent_id="agent-7", reservation_id=RESERVATION,
            bin="b0", leader_epoch=3, **call()),
        rec(c, "ie", "routing.assigned", "agent assigned", step=2, component="ie_routing_client::client",
            cause="rpc:parity.routing.v1.RoutingService/Watch:Assigned", agent_id="agent-7",
            reservation_id=RESERVATION, **call(**ie)),
        rec(c, "tc", "leg.answered", "leg answered", step=900, component="parity.telephonycontroller.node.Legs",
            cause="sip:200", node="media-1", leg_id=LEG_AGENT, agent_id="agent-7", **call(),
            attrs={"role": "agent"}),
        rec(c, "ie", "interaction.state_changed", "state changed", step=2, component="ie_core::machine",
            cause="rpc:parity.telephony.v1.TelephonyService/Watch:LegUpdate", from_state="queued",
            to_state="connected", agent_id="agent-7", leg_id=LEG_AGENT, **call(**ie)),
        rec(c, "tc", "leg.ended", "leg ended", step=184000, component="parity.telephonycontroller.node.Legs",
            cause="sip:BYE", node="media-1", leg_id=LEG_CUSTOMER, **call(), attrs={"sip_status": 200}),
        rec(c, "ie", "interaction.ended", "interaction ended", step=3, component="ie_core::machine",
            cause="rpc:parity.telephony.v1.TelephonyService/Watch:SessionEnded", **call(**ie),
            attrs={"end_reason": "completed", "duration_s": 189}),
        rec(c, "ie", "cdr.written", "cdr written", step=8, component="interaction_engine::cdr", outcome="ok",
            duration_ms=3.0, **call(**ie)),
        rec(c, "re", "agent.state_changed", "agent state changed", step=1, component="routing/agents",
            agent_id="agent-7", from_state="on_call", to_state="wrap_up", **call()),
    ]


def consult_flow():
    c = Clock()
    c.ms = 20_000  # 13 s after the call above connected
    ie = {"bin": "b0", "owner_epoch": 41}
    child = call(CONSULT_XSID, parent_xsid=XSID)
    return [
        rec(c, "ie", "consult.started", "consult started", step=0, component="ie_core::consult",
            cause="cmd:" + IE_CMD, agent_id="agent-7", command_id=IE_CMD, **child, **ie,
            attrs={"target": "agent"}),
        rec(c, "ie", "interaction.state_changed", "state changed", component="ie_core::machine",
            cause="cmd:" + IE_CMD, from_state="connected", to_state="consulting", **call(**ie),
            attrs={"consult_xsid": CONSULT_XSID}),
        rec(c, "re", "entry.enqueued", "caller queued", step=2, component="routing/queue",
            cause="rpc:parity.routing.v1.RoutingService/Enqueue", queue_id="experts", entry_id=CONSULT_ENTRY,
            bin="b0", leader_epoch=3, **child),
        rec(c, "re", "offer.sent", "offer sent", step=2, component="routing/offers", agent_id="agent-12",
            reservation_id=CONSULT_RESERVATION, queue_id="experts", **child, attrs={"timeout_ms": 15000}),
        rec(c, "tc", "leg.answered", "leg answered", step=3100, component="parity.telephonycontroller.node.Legs",
            cause="sip:200", node="media-1", leg_id=LEG_CONSULT, agent_id="agent-12", **child,
            attrs={"role": "consult"}),
        rec(c, "ie", "consult.completed", "consult completed", step=41000, component="ie_core::consult",
            cause="ws:consult_complete", leg_id=LEG_CONSULT, agent_id="agent-12", **child, **ie),
        rec(c, "ie", "leg.added", "leg joined the call", step=1, component="ie_core::legs", leg_id=LEG_CONSULT,
            agent_id="agent-12", **call(**ie), attrs={"role": "agent", "consult_xsid": CONSULT_XSID}),
        rec(c, "ie", "leg.removed", "leg left the call", step=1, component="ie_core::legs", leg_id=LEG_AGENT,
            agent_id="agent-7", **call(**ie), attrs={"reason": "transferred"}),
        rec(c, "ie", "interaction.state_changed", "state changed", step=1, component="ie_core::machine",
            from_state="consulting", to_state="ended", **child, **ie),
    ]


def ie_errors():
    c = Clock()
    return [
        rec(c, "ie", "routing.rpc_failed", "routing call failed; retrying", level="warn", step=0,
            component="ie_routing_client::client", op="parity.routing.v1.RoutingService/Enqueue", attempt=2,
            outcome="retry", duration_ms=300, queue_id="sales", **call(bin="b0"),
            error={"code": "unavailable", "kind": "tonic::Status", "message": "transport error: connection refused"},
            code={"file": "crates/ie-routing-client/src/client.rs", "line": 212,
                  "func": "ie_routing_client::client::enqueue"}),
        rec(c, "ie", "dependency.unavailable", "a dependency is unavailable", level="warn", step=10000,
            component="ie_routing_client::client", repeat_count=14, repeat_since="2026-11-01T10:00:00.400Z",
            error={"code": "unavailable", "kind": "tonic::Status"}, attrs={"dependency": "routing"}),
        rec(c, "ie", "service.panic", "panic", level="fatal", step=5, component="interaction_engine",
            error={"kind": "panic", "message": "called `Option::unwrap()` on a `None` value",
                   "stack": "panicked at crates/ie-core/src/machine.rs:88:9\n   0: ie_core::machine::Machine::apply\n"
                            "   1: interaction_engine::calls::owner::run\n   2: tokio::runtime::task::harness::poll"},
            code={"file": "crates/ie-core/src/machine.rs", "line": 88}),
    ]


def tc_commands():
    c = Clock()
    return [
        rec(c, "tc", "command.executed", "command executed", step=0,
            component="parity.telephonycontroller.grpc.TelephonyApi", thread="grpc-default-executor-3",
            op="parity.telephony.v1.TelephonyService/RingLeg", command_id=IE_CMD, outcome="ok", duration_ms=12,
            leg_id=LEG_AGENT, **call()),
        rec(c, "tc", "dtmf.received", "key press", level="debug", step=900,
            component="parity.telephonycontroller.node.Dtmf", thread="esl-1", leg_id=LEG_CUSTOMER, **call(),
            attrs={"digits": "[redacted]"}),
        rec(c, "tc", "command.failed", "command failed", level="error", step=40,
            component="parity.telephonycontroller.grpc.TelephonyApi", thread="grpc-default-executor-1",
            op="parity.telephony.v1.TelephonyService/AnswerLeg", command_id=IE_CMD, outcome="refused",
            leg_id=LEG_AGENT, **call(),
            error={"code": "invalid_state", "kind": "parity.telephonycontroller.node.InvalidState",
                   "message": "leg is not ringing",
                   "stack": "parity.telephonycontroller.node.InvalidState: leg is not ringing\n"
                            "  at parity.telephonycontroller.node.Legs.answer(Legs.java:211)\n"
                            "  at parity.telephonycontroller.grpc.TelephonyApi.answerLeg(TelephonyApi.java:140)\n"
                            "caused by: java.io.IOException: esl connection reset\n"
                            "  at parity.telephonycontroller.node.Esl.send(Esl.java:88)"}),
    ]


def web_services():
    c = Clock()
    return [
        rec(c, "fe", "http.request", "request served", step=0, component="parity.frontend.web.SignupController",
            thread="tomcat-handler-4", op="POST /signup", request_id="req-01JC2W8Q7ZK3", outcome="ok",
            duration_ms=45, trace_id=TRACE, span_id="4d5e6f708192a3b4", attrs={"status": 303}),
        rec(c, "fe", "signup.submitted", "signup submitted", step=1, component="parity.frontend.signup.Signups",
            request_id="req-01JC2W8Q7ZK3", attrs={"signup_id": "0192f3c2-0000-7000-8000-0000000051a1",
                                                  "email": "[email]"}),
        rec(c, "bl", "webhook.received", "stripe webhook received", step=800,
            component="parity.billing.stripe.Webhooks", request_id="req-01JC2W9B4M1X",
            attrs={"stripe_event_type": "invoice.paid", "stripe_event_id": "evt_1QAbCdEfGh"}),
        rec(c, "bl", "entitlement.published", "entitlements published", step=20,
            component="parity.billing.entitlements.Publisher", tenant_id=TENANT, event_id=EVENT_ID),
        rec(c, "st", "request.applied", "request applied", step=30, component="parity.store.requests.Applier",
            tenant_id=TENANT, request_id="console-0:0192f3c2-1000-7000-8000-000000000001",
            op="tenant.store.requests:set_queue", outcome="ok", duration_ms=7),
        rec(c, "ac", "call.details_viewed", "call details viewed", step=60000,
            component="parity.adminconsole.calls.CallDetails", session_id="sess-8c1f", tenant_id=TENANT,
            xsid=XSID, attrs={"user_id": "u-81"}),
    ]


def op_flow():
    c = Clock()
    rs = {"zone": "eu1a", "op_id": OP_ID, "saga_id": SAGA_ID, "tenant_id": TENANT}
    return [
        rec(c, "ct", "action.requested", "action requested", step=0, component="parity.cctools.routing.Moves",
            op_id=OP_ID, tenant_id=TENANT, request_id="cct-0:0193a1b2-0000-7000-8000-000000000001",
            attrs={"action": "move_tenant", "actor_sub": "5f0c2a9e-1b2c-4d3e-8f40-123456789abc"}),
        rec(c, "rs", "saga.accepted", "saga accepted", step=4, component="parity.rs.saga.Sagas",
            cause="kafka:tenant.routing.ops.requests.eu1a", **rs, attrs={"kind": "unit_move"}),
        rec(c, "ct", "action.answered", "action answered", step=2, component="parity.cctools.routing.Moves",
            op_id=OP_ID, outcome="ok"),
        rec(c, "rs", "assignment.written", "assignment written", step=6, component="parity.rs.exec.Assignments",
            **rs, attrs={"unit": "bin-b", "epoch": 7710}),
        rec(c, "rs", "saga.state_changed", "saga moved", step=1, component="parity.rs.saga.Sagas",
            cause="internal:etcd", from_state="fencing", to_state="switching", **rs, attrs={"kind": "unit_move"}),
        rec(c, "rs", "placement.written", "placement written", step=40, component="parity.rs.store.StoreWriter",
            **rs, attrs={"version": 8}),
        rec(c, "rs", "saga.finished", "saga finished", step=2, component="parity.rs.saga.Sagas",
            cause="kafka:replies.parity-rs.eu1a", **rs, attrs={"kind": "unit_move", "saga_outcome": "done"}),
        rec(c, "ct", "action.outcome", "action outcome", step=5, component="parity.cctools.routing.Progress",
            op_id=OP_ID, saga_id=SAGA_ID, attrs={"saga_outcome": "done"}),
    ]


def ms_leg():
    c = Clock()
    return [
        rec(c, "ms", "leg.started", "leg media started", step=0, component="parity_ms::leg",
            leg_id=LEG_CUSTOMER, node="media-1", **call(), attrs={"codec": "PCMU", "mode": "passthrough"},
            code={"file": "src/leg/mod.rs", "line": 140}),
        rec(c, "ms", "thread.overrun", "media thread overran its cycle", level="warn", step=5000,
            component="parity_ms::cycle", repeat_count=3, repeat_since="2026-11-01T10:00:04.200Z",
            attrs={"thread_name": "rtp-2", "overrun_us": 850}),
        rec(c, "ms", "log.dropped", "log records dropped", level="warn", step=1,
            component="parity_ms::logging", attrs={"dropped": 12}),
        rec(c, "ms", "leg.stats", "leg media summary", step=184000, component="parity_ms::leg",
            leg_id=LEG_CUSTOMER, node="media-1", **call(),
            attrs={"packets_in": 9420, "packets_out": 9418, "loss_pct": 0.1, "jitter_ms": 3.2, "duration_s": 188}),
    ]


def re_decisions():
    c = Clock()
    return [
        rec(c, "re", "leader.elected", "leading the bin", step=0, component="ha/leader", bin="b0", leader_epoch=3),
        rec(c, "re", "agent.selected", "agent selected", step=40, component="routing/match", queue_id="sales",
            entry_id=ENTRY, agent_id="agent-7", reservation_id=RESERVATION, bin="b0", leader_epoch=3, **call(),
            decision={"kind": "agent.select", "sampled": True, "total": 41, "chosen": "agent-7",
                      "reason": "best_score",
                      "candidates": [{"id": "agent-7", "score": 0.91, "parts": {"skill_match": 1.0, "idle_s": 0.6}},
                                     {"id": "agent-3", "score": 0.74, "parts": {"skill_match": 1.0, "idle_s": 0.2},
                                      "reason": "lower_score"}]}),
        rec(c, "re", "weights.updated", "queue weight moved", step=30000, component="optimizer/levers",
            queue_id="sales", bin="b0", leader_epoch=3,
            decision={"kind": "weight.update", "sampled": True, "chosen": "sales", "reason": "sla_pressure",
                      "candidates": [{"id": "sales", "score": 1.3,
                                      "parts": {"sl_gap": 0.12, "weight_before": 1.0, "weight_after": 1.3}}]},
            attrs={"weight": 1.3}),
        rec(c, "re", "request.refused", "request refused", level="warn", step=200, component="api/routing",
            op="parity.routing.v1.RoutingService/Enqueue", repeat_count=3,
            repeat_since="2026-11-01T10:00:30.050Z", tenant_id=TENANT, error={"code": "stale_owner"}),
    ]


def lifecycle():
    c = Clock()
    return [
        rec(c, "gw", "service.starting", "starting", step=0, component="parity.gateway.Main"),
        rec(c, "gw", "config.loaded", "configuration loaded", component="parity.gateway.Config",
            attrs={"file": "/etc/parity-gateway/application.yml"}),
        rec(c, "gw", "service.ready", "ready", step=900, component="parity.gateway.Readiness"),
        rec(c, "gw", "service.draining", "draining", step=60000, component="parity.gateway.Readiness",
            cause="signal:SIGTERM"),
        rec(c, "gw", "service.stopped", "exiting", step=5000, component="parity.gateway.Main",
            attrs={"exit_code": 0}),
    ]


def one(who, event, msg, level="info", **fields):
    return [rec(Clock(), who, event, msg, level=level, step=0, **fields)]


def invalid():
    """(name, description, catalogues, records or line text, violates)."""
    offer = dict(agent_id="agent-7", reservation_id=RESERVATION)
    big = [{"id": f"agent-{i}", "score": 0.5, "eligible": True, "reason": "lower_score_than_the_chosen_one" * 4,
            "parts": {f"term_{j}_with_a_long_descriptive_name": 0.1 for j in range(12)}} for i in range(16)]
    return [
        ("not-json", "A plain-text line (a framework's banner or a println).", ["parity-gateway"],
         "2026-11-01 10:00:00 INFO  starting parity-gateway", ["not_json"]),
        ("line-too-long", "A record over 16000 bytes: Docker would split it.", ["parity-gateway"],
         one("gw", "service.starting", "starting", attrs={"note": "x" * 16100}), ["line_too_long"]),
        ("level-uppercase", "level INFO: today's Java and Rust formatters write uppercase levels.",
         ["parity-gateway"], one("gw", "service.ready", "ready", level="INFO"), ["schema"]),
        ("message-not-msg", "A top-level field the schema doesn't have (message, today's name for msg).",
         ["parity-gateway"], [dict(one("gw", "service.ready", "ready")[0], message="ready")], ["schema"]),
        ("cause-is-an-exception", "cause holding an exception class, as the Java services write it today"
         " (that is error.kind).", ["parity-gateway"],
         one("gw", "service.ready", "ready", cause="IOException"), ["schema"]),
        ("xsid-v4", "An xsid that isn't UUIDv7 (opensips' $uuid today).", ["parity-cc-routing"],
         one("re", "offer.sent", "offer sent", tenant_id=TENANT, xsid=XSID_V4, **offer,
             attrs={"timeout_ms": 15000}), ["bad_xsid"]),
        ("xsid-uppercase", "An xsid in uppercase.", ["parity-cc-routing"],
         one("re", "offer.sent", "offer sent", tenant_id=TENANT, xsid=XSID.upper(), **offer,
             attrs={"timeout_ms": 15000}), ["bad_xsid"]),
        ("parent-is-self", "parent_xsid equal to xsid.", ["parity-interaction-engine"],
         one("ie", "consult.started", "consult started", tenant_id=TENANT, xsid=XSID, parent_xsid=XSID,
             agent_id="agent-7"), ["bad_xsid"]),
        ("call-event-without-xsid", "A call-scoped event (offer.sent) without xsid, though it has every"
         " other id.", ["parity-cc-routing"],
         one("re", "offer.sent", "offer sent", tenant_id=TENANT, **offer, attrs={"timeout_ms": 15000}),
         ["missing_xsid"]),
        ("interaction-id-differs", "interaction_id and xsid differ.", ["parity-cc-routing"],
         one("re", "offer.sent", "offer sent", **call(interaction_id=CONSULT_XSID), **offer,
             attrs={"timeout_ms": 15000}), ["xsid_mismatch"]),
        ("leg-event-without-leg-id", "A call-path event without the correlation its catalogue requires"
         " (leg.answered without leg_id).", ["parity-telephony-controller"],
         one("tc", "leg.answered", "leg answered", **call(), attrs={"role": "agent"}), ["missing_correlation"]),
        ("call-event-without-tenant", "A call-scoped event without tenant_id.", ["parity-cc-routing"],
         one("re", "offer.sent", "offer sent", xsid=XSID, **offer, attrs={"timeout_ms": 15000}),
         ["missing_correlation"]),
        ("offer-without-timeout", "offer.sent without its required attrs.timeout_ms.", ["parity-cc-routing"],
         one("re", "offer.sent", "offer sent", **call(), **offer), ["missing_attr"]),
        ("unknown-service", "A service no catalogue given names.", ["parity-gateway"],
         [dict(one("gw", "service.ready", "ready")[0], service="parity-unknown")], ["unknown_service"]),
        ("unknown-event", "An event its service's catalogue doesn't list.", ["parity-cc-routing"],
         one("re", "offer.sended", "offer sent", **call(), **offer, attrs={"timeout_ms": 15000}),
         ["unknown_event"]),
        ("level-not-allowed", "interaction.created at debug: its catalogue allows info only.",
         ["parity-interaction-engine"],
         one("ie", "interaction.created", "interaction created", level="debug", **call(bin="b0", owner_epoch=41),
             attrs={"origin": "pstn"}), ["level_not_allowed"]),
        ("transition-without-from", "A transition event with to_state only.", ["parity-interaction-engine"],
         one("ie", "interaction.state_changed", "state changed", to_state="queued", **call()),
         ["transition_incomplete"]),
        ("decision-too-large", "A decision of 16 candidates with every score part: over 4096 bytes.",
         ["parity-cc-routing"],
         one("re", "agent.selected", "agent selected", level="debug", queue_id="sales", entry_id=ENTRY,
             agent_id="agent-0", reservation_id=RESERVATION, **call(),
             decision={"kind": "agent.select", "total": 16, "chosen": "agent-0", "reason": "best_score",
                       "candidates": big}), ["decision_too_large"]),
        ("decision-at-info-unsampled", "A decision at info without sampled: true.", ["parity-cc-routing"],
         one("re", "agent.selected", "agent selected", queue_id="sales", entry_id=ENTRY, agent_id="agent-7",
             reservation_id=RESERVATION, **call(),
             decision={"kind": "agent.select", "chosen": "agent-7"}), ["decision_level"]),
        ("decision-on-plain-event", "A decision on an event whose catalogue entry has no decision.",
         ["parity-cc-routing"],
         one("re", "offer.sent", "offer sent", level="info", **call(), **offer, attrs={"timeout_ms": 15000},
             decision={"kind": "agent.select", "sampled": True, "chosen": "agent-7"}), ["decision_level"]),
        ("stack-at-warn", "error.stack on a warn record.", ["parity-interaction-engine"],
         one("ie", "routing.rpc_failed", "routing call failed; retrying", level="warn",
             op="parity.routing.v1.RoutingService/Enqueue", attempt=2, outcome="retry", **call(),
             error={"code": "unavailable", "stack": "at ie_routing_client::client::enqueue"}), ["stack_level"]),
        ("unmasked-phone-attr", "attrs.from holding a whole number (a phone-class name).",
         ["parity-telephony-controller"],
         one("tc", "session.arrived", "call arrived from the edge", node="media-1", leg_id=LEG_CUSTOMER, **call(),
             attrs={"direction": "inbound", "from": "+447700900123"}), ["unmasked_phone"]),
        ("unmasked-phone-in-text", "A number inside free text (error.message).", ["parity-telephony-controller"],
         one("tc", "command.failed", "command failed", level="warn", op="parity.telephony.v1.TelephonyService/RingLeg",
             command_id=IE_CMD, **call(),
             error={"code": "invalid_request", "message": "no route for sip:+447700900123@carrier.example"}),
         ["unmasked_phone"]),
        ("dtmf-digit", "A DTMF digit (attrs.digits) in clear: PCI.", ["parity-telephony-controller"],
         one("tc", "dtmf.received", "key press", level="debug", leg_id=LEG_CUSTOMER, **call(),
             attrs={"digits": "5"}), ["unmasked_secret"]),
        ("unmasked-email-attr", "An email address under an email-class name.", ["parity-billing"],
         one("bl", "payment.failed", "payment failed", level="warn", tenant_id=TENANT,
             error={"code": "card_declined"},
             attrs={"billing_account_id": "ba-31", "billing_email": "pat@example.com"}), ["unmasked_email"]),
        ("email-in-msg", "An email address interpolated into msg.", ["parity-frontend"],
         one("fe", "email.sent", "verification sent to pat@example.com", attrs={"email_kind": "verify"}),
         ["unmasked_email"]),
        ("seq-repeated", "Two records of one process with the same seq.", ["parity-gateway"],
         one("gw", "service.starting", "starting") + one("gw", "service.ready", "ready"), ["seq_repeat"]),
    ]


RULE_CODES = ["not_json", "line_too_long", "schema", "bad_xsid", "missing_xsid", "xsid_mismatch",
              "missing_correlation", "missing_attr", "unknown_service", "unknown_event", "level_not_allowed",
              "transition_incomplete", "decision_too_large", "decision_level", "stack_level", "unmasked_phone",
              "unmasked_secret", "unmasked_email", "seq_repeat"]


def fixture_kind():
    """The 'log' kind in schemas/fixtures/fixture.v1.json (idempotent)."""
    path = ROOT / "schemas/fixtures/fixture.v1.json"
    doc = json.loads(path.read_text())
    doc["$defs"]["log"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["description", "kind", "writer_valid", "violates", "catalogues"],
        "properties": {
            "description": {"type": "string", "minLength": 1},
            "kind": {"const": "log"},
            "writer_valid": {"type": "boolean"},
            "violates": {"type": "array", "uniqueItems": True, "items": {"enum": RULE_CODES}},
            "catalogues": {
                "description": "Service catalogues (paths from the repository root); logging/events.common.v1.json"
                               " always applies",
                "type": "array", "minItems": 1, "uniqueItems": True,
                "items": {"type": "string", "pattern": "^logging/[A-Za-z0-9._/-]+\\.json$"}},
            "records": {"description": "The lines, as objects (each written compactly is one NDJSON line)",
                        "type": "array", "minItems": 1, "items": {"type": "object"}},
            "line_text": {"description": "One raw line, for what isn't a JSON object", "type": "string"},
        },
        "oneOf": [{"required": ["records"]}, {"required": ["line_text"]}],
        "if": {"properties": {"writer_valid": {"const": True}}},
        "then": {"properties": {"violates": {"maxItems": 0}}, "required": ["records"]},
        "else": {"properties": {"violates": {"minItems": 1}}},
    }
    ref = {"$ref": "#/$defs/log"}
    if ref not in doc["oneOf"]:
        doc["oneOf"].append(ref)
    marker = (" Since 0.13.0 also log lines (kind log), checked against schemas/logging/log-record.v1.json, the"
              " catalogues named and CONVENTIONS.md's \"Logging (log-record.v1)\".")
    if marker not in doc["description"]:
        doc["description"] += marker
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def write(rel, doc):
    p = ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


SINCE = {"parity-rs": "0.16.0", "parity-cc-tools": "0.16.0"}


def main():
    fixture_kind()
    write("logging/events.common.v1.json", catalogue(
        "*", COMMON, "Events every service writes (normative). A service's own catalogue must not repeat"
        " these names; contract-check merges this file into every catalogue."))
    for service, events in CATALOGUES.items():
        write(cat_path(service), catalogue(
            service, events, f"Starter catalogue for {service} (parity-contracts {SINCE.get(service, '0.13.0')})."
            " Copy it to the"
            f" service's repository as logging/events.{service}.v1.json; the service owns it from then on."))
    if FIX.exists():
        shutil.rmtree(FIX)
    families = [
        ("call-across-services", "One inbound call across TC, IE, the routing engine and the gateway, joined by"
         " xsid (and trace_id): arrival, IVR, queue, decision, offer, answer, connect, hang-up, CDR.",
         ["parity-telephony-controller", "parity-interaction-engine", "parity-cc-routing", "parity-gateway"],
         call_flow()),
        ("consult-child-call", "A consult: a child call with its own xsid and parent_xsid; on completion the"
         " consulted agent's leg joins the parent call, which keeps its xsid.",
         ["parity-interaction-engine", "parity-cc-routing", "parity-telephony-controller"], consult_flow()),
        ("rust-ie-errors", "Rust (tracing): a retried RPC, a deduplicated dependency error (repeat_count) and a"
         " panic with its stack.", ["parity-interaction-engine"], ie_errors()),
        ("java-tc-commands", "Java (Spring Boot, logback): commands with thread and incarnation, a masked DTMF"
         " field, an error with a multi-line stack in one string.", ["parity-telephony-controller"], tc_commands()),
        ("java-web-services", "Java web and Kafka services: frontend, billing, store, admin-console (a tenant"
         " user opening a call by its xsid).",
         ["parity-frontend", "parity-billing", "parity-store", "parity-admin-console"], web_services()),
        ("rust-ms-media", "parity-ms: one record per leg start and end (stats), never per packet; a deduplicated"
         " thread overrun; dropped log records.", ["parity-ms"], ms_leg()),
        ("cpp-re-decisions", "parity-cc-routing (C++): a sampled agent decision at info, an optimizer decision,"
         " a rate-limited refusal.", ["parity-cc-routing"], re_decisions()),
        ("op-across-services", "One operator action (0.16.0): parity-cc-tools asks parity-rs to move a tenant;"
         " every record of the operation carries its op_id (and saga_id), as a call's carry its xsid.",
         ["parity-cc-tools", "parity-rs"], op_flow()),
        ("lifecycle", "The common lifecycle events (logging/events.common.v1.json).", ["parity-gateway"],
         lifecycle()),
    ]
    for name, description, cats, records in families:
        write(f"fixtures/log-record.v1/valid/{name}.json", fixture(description, True, cats, records=records))
    for name, description, cats, body, violates in invalid():
        if isinstance(body, str):
            f = fixture(description, False, cats, line_text=body, violates=violates)
        else:
            f = fixture(description, False, cats, records=body, violates=violates)
        write(f"fixtures/log-record.v1/invalid/{name}.json", f)


if __name__ == "__main__":
    main()
