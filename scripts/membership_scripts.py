#!/usr/bin/env python3
"""Writes parity-cc-routing's agents.v1 scripts of 0.11.0 (its PLAN §4.5): agents moving between
queues (active sets with tiers, pins, moves, dwell, the "next offer, never mid-call" rule, tenant
isolation of plans and pins) and a supervisor's forced state. They need routing-engine stimuli
(config_routing, config_routing_pin, moves, call_connected) that only RE's harness has, so the
gateway's fake doesn't play them. gateway_contracts.py calls main() after it rewrites
fixtures/agents.v1/. Run alone: python3 -I scripts/membership_scripts.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gateway_contracts as g  # noqa: E402

A = g.A
T0 = g.T0
TENANT, OTHER = g.TENANT, g.OTHER
A1, A2 = g.AGENT, g.AGENT_B
AG = "agent-g01"  # a globex agent
S1, S2 = g.SESSION, g.SESSION2
I1 = g.INTERACTION
I2 = "0192f6a1-7c3e-7b21-9a4d-5e6f70819204"
I3 = "0192f6a1-7c3e-7b21-9a4d-5e6f70819205"
OFFER, OFFER2 = g.OFFER, "0192f6a1-6000-7aaa-8bbb-000000000002"
RES = g.RES
P, S = "MEMBERSHIP_TIER_PRIMARY", "MEMBERSHIP_TIER_SECONDARY"


def m(queue, tier, pinned=False):
    x = {"queue_id": queue, "tier": tier}
    if pinned:
        x["pinned"] = True
    return x


def view(state, version, reason=None, queues=None, pending=None, qv=0, pending_target=None):
    v = g.view(state, version, reason, pending_target)
    if queues is not None:
        v["queues"] = queues
    if pending:
        v["pending_queues"] = pending
    if qv:
        v["queues_version"] = str(qv)
    return v


def deliver(seq, agent=A1, **event):
    d = g.deliver(seq, **event)
    d["body"]["agent_id"] = agent
    return d


def queues_changed(seq, queues, reason, qv, pending=None, agent=A1):
    ev = {"queues": queues, "reason": f"MEMBERSHIP_REASON_{reason}", "queues_version": str(qv)}
    if pending:
        ev["pending_queues"] = pending
    return deliver(seq, agent, queues_changed=ev)


def offer(interaction, queue, expires, tier=None, offer_id=OFFER):
    o = {"offer_id": offer_id, "interaction_id": interaction, "channel": "CHANNEL_VOICE", "queue_id": queue,
         "expires_at_ms": str(expires)}
    if tier:
        o["tier"] = tier
    return o


def sess(agent=A1, session=S1):
    return {"tenant_id": TENANT, "agent_id": agent, "session_id": session, "attach_generation": "1"}


def attach(agent, session, state, reservations=None):
    body = {"attach_generation": "1", "existing": False, "agent": state, "leader_epoch": "7"}
    if reservations:
        body["reservations"] = reservations
    return g.ag("AttachAgent", {"tenant_id": TENANT, "agent_id": agent, "replica_id": g.REPLICA, "session_id": session,
                                "agent_contact": f"sip:{agent}@acme.agents.example"}, g.ok(A, "AttachAgent", body))


def cid(n, agent=A1):
    return g.command_id(TENANT, agent, f"0192f6a1-c100-7aaa-8bbb-{n:012d}")


def inject(what, **args):
    return {"op": "inject", "what": what, "args": args}


def queue_call(interaction, queue, priority=None):
    a = {"tenant_id": TENANT, "interaction_id": interaction, "queue_id": queue}
    if priority:
        a["priority"] = priority
    return {"op": "inject", "what": "queue_call", "args": a}


def moves(expect, *mv):
    return inject("moves", tenant_id=TENANT, moves=[{"agent_id": a, "queues": q} for a, q in mv], expect=expect)


def mq(queue, tier, weight=None):
    x = {"queue_id": queue, "tier": tier}
    if weight is not None:
        x["weight"] = weight
    return x


def pin(agent, version, queues=None, tombstone=False, expect=None, key=None, tenant=TENANT):
    doc = {"tenant_id": tenant, "agent_id": agent}
    if tombstone:
        doc["tombstone"] = True
    else:
        doc.update({"version": version, "queues": [{"queue_id": q, "tier": t} for q, t in queues],
                    "reason_code": "supervisor"})
    args = {"doc": doc, "expect": expect or {"applied": True}}
    if key:
        args["key"] = key
    return {"op": "inject", "what": "config_routing_pin", "args": args}


def given(agents, queues=("sales", "billing"), routing=None, pins=None, tenants=(TENANT,)):
    gv = {"start_ms": str(T0), "leader_epoch": "7", "offer_timeout_ms": "15000", "grace_ms": "60000",
          "queues": [{"tenant_id": t, "queue_id": q} for t in tenants for q in queues], "agents": agents}
    gv["routing"] = routing if routing is not None else [{"tenant_id": TENANT, "version": 1, "mode": "auto"}]
    if pins:
        gv["routing_pins"] = pins
    return gv


def agent(agent_id, state, queues=("sales", "billing"), reason=None, reservation=None, tenant=TENANT):
    a = {"tenant_id": tenant, "agent_id": agent_id, "state": state, "queues": list(queues)}
    if reason:
        a["reason_code"] = reason
    if reservation:
        a["reservation"] = reservation
    return a


def answer(n, decision, result, agent=A1, session=S1, offer_id=OFFER):
    return g.ag("AnswerOffer", {"session": sess(agent, session), "command_id": cid(n, agent), "offer_id": offer_id,
                                "decision": f"DECISION_{decision}"}, g.ok(A, "AnswerOffer", result))


BOTH = [m("billing", P), m("sales", P)]
BILLING = [m("billing", P)]


def scripts():
    acc = {"decision": "DECISION_ACCEPT", "reservation": {"reservation_id": RES, "interaction_id": I2,
                                                          "origin": "RESERVATION_ORIGIN_INBOUND"}}
    g.script(A, "move-ready-agent-next-offer",
             "0.11.0: a READY agent moved from sales to billing gets its next offer from billing; QueuesChanged first", [
                 g.watch(),
                 attach(A1, S1, view("READY", 2, queues=BOTH)),
                 attach(A2, S2, view("NOT_READY", 1, "login")),
                 moves({"applied": 1}, (A1, [mq("billing", P)])),
                 queues_changed(1, BILLING, "OPTIMIZER", 1),
                 queue_call(I1, "sales"),
                 queue_call(I2, "billing"),
                 deliver(2, offer=offer(I2, "billing", T0 + 15000, P)),
                 answer(1, "ACCEPT", acc),
             ], given([agent(A1, "READY"), agent(A2, "NOT_READY", reason="login")]))

    on_call = {"reservation_id": RES, "interaction_id": I1}
    g.script(A, "race-move-while-on-call",
             "0.11.0: a move during ON_CALL stays pending through wrap-up and applies at READY; the call untouched", [
                 g.watch(),
                 attach(A1, S1, view("ON_CALL", 5, queues=BOTH), reservations=[on_call]),
                 attach(A2, S2, view("NOT_READY", 1, "login")),
                 moves({"pending": 1}, (A1, [mq("billing", P)])),
                 queues_changed(1, BOTH, "OPTIMIZER", 1, pending=BILLING),
                 inject("call_ended", tenant_id=TENANT, reservation_id=RES),
                 deliver(2, wrap_up_started={"reservation_id": RES, "interaction_id": I1, "ends_at_ms": str(T0 + 60000)}),
                 deliver(3, state_changed={"agent": view("WRAP_UP", 6, queues=BOTH, pending=BILLING, qv=1)}),
                 g.ag("EndWrapUp", {"session": sess(), "command_id": cid(2), "reservation_id": RES, "disposition_code": "sale"},
                      g.ok(A, "EndWrapUp", {"agent": view("READY", 7, queues=BILLING, qv=2)})),
                 queues_changed(4, BILLING, "OPTIMIZER", 2),
                 queue_call(I2, "sales"),
                 queue_call(I3, "billing"),
                 deliver(5, offer=offer(I3, "billing", T0 + 15000, P)),
             ], given([agent(A1, "ON_CALL", reservation=on_call), agent(A2, "NOT_READY", reason="login")]))

    g.script(A, "race-move-while-offered",
             "0.11.0: a move during OFFERED doesn't revoke the offer; after the reject the new set applies", [
                 g.watch(),
                 attach(A1, S1, view("READY", 2, queues=BOTH)),
                 attach(A2, S2, view("NOT_READY", 1, "login")),
                 queue_call(I1, "sales"),
                 deliver(1, offer=offer(I1, "sales", T0 + 15000, P)),
                 moves({"pending": 1}, (A1, [mq("billing", P)])),
                 queues_changed(2, BOTH, "OPTIMIZER", 1, pending=BILLING),
                 answer(3, "REJECT", {"decision": "DECISION_REJECT"}),
                 queues_changed(3, BILLING, "OPTIMIZER", 2),
                 deliver(4, state_changed={"agent": view("READY", 4, queues=BILLING, qv=2)}),
                 queue_call(I2, "billing"),
                 deliver(5, offer=offer(I2, "billing", T0 + 15000, P, OFFER2)),
             ], given([agent(A1, "READY"), agent(A2, "NOT_READY", reason="login")]))

    g.script(A, "dwell-blocks-second-move",
             "0.11.0: a second move inside the dwell time is not applied; a supervisor's pin is", [
                 g.watch(),
                 attach(A1, S1, view("READY", 2, queues=BOTH)),
                 attach(A2, S2, view("NOT_READY", 1, "login")),
                 moves({"applied": 1}, (A1, [mq("billing", P)])),
                 queues_changed(1, BILLING, "OPTIMIZER", 1),
                 {"op": "advance", "ms": "60000"},
                 moves({"refused": 1, "why": [f"{A1}: dwell"]}, (A1, [mq("sales", P)])),
                 pin(A1, 1, [("sales", "primary")]),
                 queues_changed(2, [m("sales", P, pinned=True)], "SUPERVISOR_PIN", 2),
             ], given([agent(A1, "READY"), agent(A2, "NOT_READY", reason="login")],
                      routing=[{"tenant_id": TENANT, "version": 1, "mode": "auto", "dwell_min_s": 300}]))

    g.script(A, "pin-overrides-plan",
             "0.11.0: a pinned agent keeps its queues whatever the plan says; the unpin gives it back", [
                 g.watch(),
                 attach(A1, S1, view("READY", 2, queues=BOTH)),
                 attach(A2, S2, view("NOT_READY", 1, "login")),
                 pin(A1, 1, [("billing", "primary")]),
                 queues_changed(1, [m("billing", P, pinned=True)], "SUPERVISOR_PIN", 1),
                 moves({"refused": 1, "why": [f"{A1}: pinned"]}, (A1, [mq("sales", P)])),
                 pin(A1, 0, tombstone=True),
                 queues_changed(2, BOTH, "SUPERVISOR_UNPIN", 2),
             ], given([agent(A1, "READY"), agent(A2, "NOT_READY", reason="login")]))

    acc1 = {"decision": "DECISION_ACCEPT", "reservation": {"reservation_id": RES, "interaction_id": I1,
                                                           "origin": "RESERVATION_ORIGIN_INBOUND"}}
    res1 = {"reservation_id": RES, "interaction_id": I1, "origin": "RESERVATION_ORIGIN_INBOUND"}
    a1_set = [m("billing", S, pinned=True), m("sales", P, pinned=True)]
    g.script(A, "secondary-after-delay",
             "0.11.0: a secondary takes a call only after the queue's delay; a primary who becomes ready first takes it", [
                 g.watch(),
                 attach(A1, S1, view("READY", 2, queues=a1_set)),
                 attach(A2, S2, view("NOT_READY", 1, "login", queues=BILLING)),
                 queue_call(I1, "billing"),
                 {"op": "advance", "ms": "9000"},
                 {"op": "advance", "ms": "1000"},
                 deliver(1, offer=offer(I1, "billing", T0 + 25000, S)),
                 answer(4, "ACCEPT", acc1),
                 deliver(2, reserved=res1),
                 deliver(3, state_changed={"agent": view("RESERVED", 4, queues=a1_set)}),
                 queue_call(I2, "billing"),
                 g.ag("SetState", {"session": sess(A2, S2), "command_id": cid(5, A2), "target": "TARGET_STATE_READY"},
                      g.ok(A, "SetState", {"agent": view("OFFERED", 3, queues=BILLING)})),
                 deliver(1, A2, offer=offer(I2, "billing", T0 + 25000, P, OFFER2)),
             ], given([agent(A1, "READY"), agent(A2, "NOT_READY", queues=("billing",), reason="login")],
                      routing=[{"tenant_id": TENANT, "version": 1, "mode": "auto",
                                "queues": [{"queue_id": "billing", "secondary_delay_s": 10}]}],
                      pins=[{"tenant_id": TENANT, "agent_id": A1, "version": 1,
                             "queues": [{"queue_id": "sales", "tier": "primary"}, {"queue_id": "billing", "tier": "secondary"}]}]))

    acc_s = {"decision": "DECISION_ACCEPT", "reservation": {"reservation_id": RES, "interaction_id": I1,
                                                            "origin": "RESERVATION_ORIGIN_INBOUND"}}
    g.script(A, "race-foreign-agent-in-plan",
             "0.11.0: a plan or a pin naming another tenant's agent, or keyed for another tenant, changes nothing", [
                 g.watch(),
                 attach(A1, S1, view("READY", 2, queues=[m("sales", P)])),
                 moves({"refused": 1, "why": [f"{AG}: not an agent of {TENANT}"]}, (AG, [mq("sales", P)])),
                 pin(AG, 1, [("sales", "primary")], expect={"applied": False}),
                 pin(A1, 1, [("sales", "secondary")], expect={"applied": False}, key=f"{TENANT}/{A1}", tenant=OTHER),
                 queue_call(I1, "sales"),
                 deliver(1, offer=offer(I1, "sales", T0 + 15000, P)),
                 answer(6, "ACCEPT", acc_s),
             ], given([agent(A1, "READY", queues=("sales",)), agent(AG, "READY", queues=("sales",), tenant=OTHER)],
                      queues=("sales",), tenants=(TENANT, OTHER)))

    g.script(A, "no-starvation-max-wait",
             "0.11.0: a call past escalate_after wins over a higher priority and a higher weight, and reaches an "
             "eligible agent outside its active set", [
                 g.watch(),
                 attach(A1, S1, view("NOT_READY", 1, "login", queues=BOTH)),
                 attach(A2, S2, view("NOT_READY", 1, "login", queues=[m("sales", P)])),
                 moves({"applied": 1}, (A1, [mq("billing", P, 4)])),
                 queues_changed(1, BILLING, "OPTIMIZER", 1),
                 queue_call(I1, "sales", 10),
                 {"op": "advance", "ms": "30000"},
                 queue_call(I2, "billing", 100),
                 g.ag("SetState", {"session": sess(), "command_id": cid(7), "target": "TARGET_STATE_READY"},
                      g.ok(A, "SetState", {"agent": view("OFFERED", 3, queues=BILLING, qv=1)})),
                 deliver(2, offer=offer(I1, "sales", T0 + 45000)),
             ], given([agent(A1, "NOT_READY", reason="login"), agent(A2, "NOT_READY", queues=("sales",), reason="login")],
                      routing=[{"tenant_id": TENANT, "version": 1, "mode": "auto",
                                "queues": [{"queue_id": "sales", "escalate_after_s": 30}]}]))

    res_i1 = {"reservation_id": RES, "interaction_id": I1, "origin": "RESERVATION_ORIGIN_INBOUND"}
    force = {"tenant_id": TENANT, "agent_id": A1, "command_id": cid(8), "target": "TARGET_STATE_NOT_READY",
             "reason_code": "supervisor", "supervisor_id": "supervisor-1"}
    g.script(A, "force-state-revokes-reservation",
             "0.11.0: a supervisor forces a reserved agent not ready (the reservation is revoked), then logs it out", [
                 g.watch(),
                 attach(A1, S1, view("READY", 2, queues=[m("sales", P)])),
                 queue_call(I1, "sales"),
                 deliver(1, offer=offer(I1, "sales", T0 + 15000, P)),
                 answer(9, "ACCEPT", {"decision": "DECISION_ACCEPT", "reservation": res_i1}),
                 deliver(2, reserved=res_i1),
                 deliver(3, state_changed={"agent": view("RESERVED", 4)}),
                 g.ag("ForceState", force, g.ok(A, "ForceState", {"agent": view("NOT_READY", 5, "supervisor")})),
                 deliver(4, state_changed={"agent": view("NOT_READY", 5, "supervisor")}),
                 g.ag("ForceState", {**force, "command_id": cid(10), "target": "TARGET_STATE_LOGGED_OUT", "reason_code": ""},
                      g.ok(A, "ForceState", {"agent": view("LOGGED_OUT", 6)})),
                 deliver(5, state_changed={"agent": view("LOGGED_OUT", 6)}),
                 deliver(6, detached={"reason": "DETACHED_REASON_FORCED_LOGOUT"}),
                 g.ag("ForceState", {**force, "command_id": cid(10), "target": "TARGET_STATE_LOGGED_OUT", "reason_code": ""},
                      g.ok(A, "ForceState", {"agent": view("LOGGED_OUT", 6)}), note="a repeat: the first answer"),
             ], given([agent(A1, "READY", queues=("sales",))], queues=("sales",)))


def main():
    scripts()
    return 0


if __name__ == "__main__":
    sys.exit(main())
