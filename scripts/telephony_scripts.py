#!/usr/bin/env python3
"""Writes fixtures/telephony.v1/: the telephony.v1 golden scripts (TC ADR 0001 §5-§6).

The scripts are data; this file is how they were written, so the owners (TC and IE) can change one
and regenerate the set. Run from the repository root: python3 -I scripts/telephony_scripts.py
Every script is checked by contract-check and played by TC's runners against
fake-telephony-controller and TC's own server.
"""
import json
import pathlib
import shutil
import sys

PKG = "parity.telephony.v1"
SVC = PKG + ".TelephonyService"
T, T2 = "acme", "globex"
I = "0192f6a1-7c3e-7b21-9a4d-5e6f70819203"        # an inbound interaction
I2 = "0192f6a1-7c3e-7b21-9a4d-5e6f70819204"       # an outbound one
I3 = "0192f6a1-7c3e-7b21-9a4d-5e6f70819205"       # another outbound one
C = "0192f6a1-8000-7000-8000-000000000001"        # the customer's leg (freeswitch's uuid)
A = "0192f6a1-8000-7000-8000-0000000000a1"        # an agent's leg (IE's UUIDv7)
A2 = "0192f6a1-8000-7000-8000-0000000000a2"
S = "0192f6a1-8000-7000-8000-0000000000c1"        # a supervisor's leg
P = "0192f6a1-8000-7000-8000-0000000000b1"        # a PSTN leg
INC1 = "0192f700-0000-7000-8000-000000000001"
INC2 = "0192f700-0000-7000-8000-000000000002"
START = 1792800000000
NODE, VERSION = "media-1", "tc-test"
R1, R2, RB = "ie-bin-a-1", "ie-bin-a-2", "ie-bin-b-1"
E1, E2 = 1042, 1052
E3 = 1062                                         # another interaction's: etcd's create revisions are unique
E4 = 1072
TP = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
CALLED, CALLER, OUT = "+447700900100", "+447700900123", "+447700900456"
SIP_CALL_ID = "a84b4c76e66710@carrier.example"
AGENT_CONTACT = "sip:agent-7f3a@acme.agents.example"
FREE = 1000
# NodeStatus.capabilities of the scripts' TC (0.10.3): AddLeg's DTMF codes, and the null recording sink
# (the fake and the test targets run it; production nodes list no sink until R9).
CAPS = ["dtmf_actions", "recording_null_sink"]


def s64(n):
    return str(n)


class Script:
    def __init__(self, name, description, pins, streams=None, peers=None, owners=None, valid=True, violates=None):
        self.name = name
        self.doc = {"description": description, "kind": "grpc", "writer_valid": valid, "package": PKG}
        if violates:
            self.doc["violates"] = violates
        self.doc["pins"] = pins
        self.doc["streams"] = streams or {"r1": {"replica_id": R1, "bin": "bin-a"}}
        if peers:
            self.doc["peers"] = peers
        self.doc["given"] = {
            "start_ms": s64(START), "incarnation": INC1, "node_id": NODE, "version": VERSION,
            "bins": ["bin-a", "bin-b"],
            "replicas": [{"bin": "bin-a", "replica_id": R1}, {"bin": "bin-a", "replica_id": R2},
                         {"bin": "bin-b", "replica_id": RB}],
            "owners": owners or [], "free_legs": FREE,
        }
        self.steps = []
        self.cmd = 0
        self.inc = INC1  # TC's run: a restart changes it, and snapshots carry it (0.2.2)

    def next_cmd(self, replica=R1):
        self.cmd += 1
        return f"{replica}:{self.cmd:04d}"

    def note(self, text):
        self.steps[-1]["note"] = text
        return self

    def watch(self, label, at=START, sessions=0, free=FREE, inc=INC1, peer=None, error=None, rebuilding=False):
        st = self.doc["streams"][label]
        step = {"op": "call", "stream": label, "method": SVC + "/Watch", "message": PKG + ".WatchRequest",
                "metadata": {"traceparent": TP}, "body": {"replica_id": st["replica_id"], "bin": st["bin"]}}
        if peer:
            step["peer"] = peer
        if error:
            step["expect"] = {"status": error[0], "parity_error": error[1]}
        else:
            ns = {"free_legs": free, "version": VERSION, "capabilities": CAPS}
            if sessions:
                ns["sessions"] = sessions
            if rebuilding:
                ns["rebuilding"] = True
            step["expect"] = {"message": PKG + ".WatchResponse",
                              "body": {"incarnation": inc, "node_id": NODE, "at_ms": s64(at), "node_status": ns}}
        self.steps.append(step)
        return self

    def inject(self, what, **args):
        self.steps.append({"op": "inject", "what": what, "args": args})
        if what in ("tc_restart", "tc_crash_mid_fence_raise"):
            self.inc = args["incarnation"]
        return self

    def advance(self, ms):
        self.steps.append({"op": "advance", "ms": s64(ms)})
        return self

    def call(self, label, rpc, body, ok=None, err=None, tenant=T, peer=None, metadata_extra=None):
        metadata = {"traceparent": TP} if tenant is None else {"tenant-id": tenant, "traceparent": TP}
        if metadata_extra:
            metadata.update(metadata_extra)
        step = {"op": "call", "stream": label, "method": f"{SVC}/{rpc}", "message": f"{PKG}.{rpc}Request",
                "metadata": metadata, "body": body}
        if peer:
            step["peer"] = peer
        if err:
            step["expect"] = {"status": err[0], "parity_error": err[1]}
        else:
            step["expect"] = {"message": f"{PKG}.{rpc}Response", "body": ok if ok is not None else {}}
        self.steps.append(step)
        return self

    def deliver(self, label, seq, at, event, epoch, inc=INC1, tenant=T, interaction=I):
        body = {"incarnation": inc, "node_id": NODE, "delivery_seq": s64(seq),
                "session": {"tenant_id": tenant, "interaction_id": interaction, "owner_epoch": s64(epoch)},
                "at_ms": s64(at)}
        body.update(event)
        self.steps.append({"op": "deliver", "stream": label, "message": PKG + ".WatchResponse", "body": body})
        return self

    def heartbeat(self, label, at, inc=INC1):
        self.steps.append({"op": "deliver", "stream": label, "message": PKG + ".WatchResponse",
                           "body": {"incarnation": inc, "node_id": NODE, "at_ms": s64(at), "heartbeat": {}}})
        return self

    def node_status(self, label, at, sessions=0, free=FREE, inc=INC1):
        ns = {"free_legs": free, "version": VERSION, "capabilities": CAPS}
        if sessions:
            ns["sessions"] = sessions
        self.steps.append({"op": "deliver", "stream": label, "message": PKG + ".WatchResponse",
                           "body": {"incarnation": inc, "node_id": NODE, "at_ms": s64(at), "node_status": ns}})
        return self

    def doc_out(self):
        d = dict(self.doc)
        d["steps"] = self.steps
        return d


# ---- message bodies -------------------------------------------------------------------------

def owner(epoch, replica=R1, acked=0, inc=INC1, tenant=T, interaction=I):
    o = {"tenant_id": tenant, "interaction_id": interaction, "owner_epoch": s64(epoch), "replica_id": replica}
    if acked:
        o["acked_delivery_seq"] = s64(acked)
        o["acked_incarnation"] = inc
    return o


def owner_rec(epoch, replica=R1, bin_="bin-a", node=NODE, interaction=I, tenant=T):
    return {"interaction_id": interaction, "owner_epoch": s64(epoch), "tenant_id": tenant, "bin": bin_,
            "replica_id": replica, "node_id": node}


def arrived(attempt=1, deadline=START + 5000):
    return {"arrived": {"bin": "bin-a", "leg_id": C, "sip_call_id": SIP_CALL_ID, "called_number": CALLED,
                        "caller_number": CALLER, "attempt": attempt, "deadline_ms": s64(deadline),
                        "trunk_id": "carrier-a"}}


def leg(leg_id, role, state, cause=None, sip=0, by=None, answered=0):
    body = {"leg_id": leg_id, "role": "LEG_ROLE_" + role, "state": "LEG_STATE_" + state}
    if cause:
        body["cause"] = "END_CAUSE_" + cause
    if sip:
        body["sip_status"] = sip
    if by:
        body["ended_by"] = "ENDED_BY_" + by
    if answered:
        body["answered_at_ms"] = s64(answered)
    return {"leg": body}


def ended(reason, sip=0):
    body = {"reason": "SESSION_END_REASON_" + reason}
    if sip:
        body["sip_status"] = sip
    return {"session_ended": body}


def snap_leg(leg_id, role, state, endpoint=None, sip_call_id=None, cause=None, sip=0, by=None, answered=0, ended_at=0):
    body = {"leg_id": leg_id, "role": "LEG_ROLE_" + role, "state": "LEG_STATE_" + state}
    if endpoint:
        body["endpoint"] = "ENDPOINT_" + endpoint
    body["sip_call_id"] = sip_call_id if sip_call_id is not None else leg_id
    if cause:
        body["cause"] = "END_CAUSE_" + cause
    if sip:
        body["sip_status"] = sip
    if by:
        body["ended_by"] = "ENDED_BY_" + by
    if answered:
        body["answered_at_ms"] = s64(answered)
    if ended_at:
        body["ended_at_ms"] = s64(ended_at)
    return body


def customer(state="ANSWERED", **kw):
    kw.setdefault("answered", START if state in ("ANSWERED",) else kw.get("answered", 0))
    return snap_leg(C, "CUSTOMER", state, endpoint="SIP", sip_call_id=SIP_CALL_ID, **kw)


def agent(leg_id=A, state="ORIGINATING", **kw):
    return snap_leg(leg_id, "AGENT", state, endpoint="WEBRTC", **kw)


def session(epoch, replica, state, media, legs, last_seq, end_reason=None, ended_at=0, deadline=0,
            tenant=T, interaction=I):
    body = {"tenant_id": tenant, "interaction_id": interaction, "owner_epoch": s64(epoch),
            "owner_replica_id": replica, "state": "SESSION_STATE_" + state, "media": "MEDIA_KIND_" + media,
            "legs": legs, "last_delivery_seq": s64(last_seq)}
    if end_reason:
        body["end_reason"] = "SESSION_END_REASON_" + end_reason
    if ended_at:
        body["ended_at_ms"] = s64(ended_at)
    if deadline:
        body["arrival_deadline_ms"] = s64(deadline)
    return body


def add_leg(own, cmd, leg_id=A, contact=AGENT_CONTACT, role="AGENT", endpoint="WEBRTC", pstn=None):
    body = {"owner": own, "command_id": cmd, "leg_id": leg_id, "role": "LEG_ROLE_" + role}
    if pstn:
        body["pstn"] = pstn
        body["caller_id"] = CALLED
    else:
        body["agent_contact"] = contact
    body.update({"ring_timeout_s": 30, "endpoint": "ENDPOINT_" + endpoint, "codec_policy": "CODEC_POLICY_G711_ONLY"})
    return body


def arrival_args(headers=None):
    a = {"leg_id": C, "interaction_id": I, "tenant_id": T, "bin": "bin-a", "called_number": CALLED,
         "caller_number": CALLER, "sip_call_id": SIP_CALL_ID, "trunk_id": "carrier-a"}
    if headers:
        a["headers"] = headers
    return a


def get(s, label, epoch, replica, state, media, legs, last_seq, tenant=T, interaction=I, **kw):
    return s.call(label, "GetSession", {"tenant_id": tenant, "interaction_id": interaction, "replica_id": replica},
                  ok={"incarnation": s.inc,
                      "session": session(epoch, replica, state, media, legs, last_seq, tenant=tenant,
                                         interaction=interaction, **kw)}, tenant=tenant)


def answered_call(s, label="r1", replica=R1):
    """Watch, arrival, claim, answer: the customer's leg answered by `replica` under E1 (seq 1, 2)."""
    s.watch(label)
    s.inject("arrival", **arrival_args())
    s.deliver(label, 1, START, arrived(), 0)
    s.inject("owner_claim", **owner_rec(E1, replica))
    s.call(label, "AnswerLeg", {"owner": owner(E1, replica, acked=1), "command_id": s.next_cmd(replica), "leg_id": C},
           ok={})
    s.deliver(label, 2, START, leg(C, "CUSTOMER", "ANSWERED", answered=START), E1)
    return s


TWO = {"r1": {"replica_id": R1, "bin": "bin-a"}, "r2": {"replica_id": R2, "bin": "bin-a"}}


def rendezvous(interaction, replicas):
    import hashlib
    def score(r):
        return int.from_bytes(hashlib.sha256(f"{interaction}/{r}".encode()).digest()[:8], "big")
    return sorted(replicas, key=lambda r: (-score(r), r))


# ---- the scripts ----------------------------------------------------------------------------

def scripts():
    out = []

    # Coverage (10).
    s = Script("watch-heartbeat-node-status", "A stream opens with node_status; a heartbeat every 2 s, node_status every 5 s", ["coverage"])
    s.watch("r1").advance(2000).heartbeat("r1", START + 2000).advance(3000).node_status("r1", START + 5000)
    out.append(s)

    s = Script("arrival-answer", "A parked call is offered (no SDP, no headers: the default allow-list is empty), claimed and answered", ["coverage", "R1-7"])
    s.watch("r1")
    s.inject("arrival", **arrival_args(headers=[{"name": "P-Asserted-Identity", "value": "<sip:+447700900123@carrier.example>"},
                                                 {"name": "User-to-User", "value": "56a390f3d2b7310023a2"}]))
    s.deliver("r1", 1, START, arrived(), 0)
    s.inject("owner_claim", **owner_rec(E1))
    s.call("r1", "AnswerLeg", {"owner": owner(E1, acked=1), "command_id": s.next_cmd(), "leg_id": C}, ok={})
    s.deliver("r1", 2, START, leg(C, "CUSTOMER", "ANSWERED", answered=START), E1)
    get(s, "r1", E1, R1, "ACTIVE", "CALL", [customer()], 2)
    out.append(s)

    s = Script("arrival-ring-answer", "180 Ringing first (a CPaaS number awaiting its verbs), then 200 OK", ["coverage"])
    s.watch("r1").inject("arrival", **arrival_args()).deliver("r1", 1, START, arrived(), 0)
    s.inject("owner_claim", **owner_rec(E1))
    s.call("r1", "RingLeg", {"owner": owner(E1, acked=1), "command_id": s.next_cmd(), "leg_id": C}, ok={})
    s.deliver("r1", 2, START, leg(C, "CUSTOMER", "RINGING"), E1)
    s.call("r1", "AnswerLeg", {"owner": owner(E1, acked=2), "command_id": s.next_cmd(), "leg_id": C}, ok={})
    s.deliver("r1", 3, START, leg(C, "CUSTOMER", "ANSWERED", answered=START), E1)
    out.append(s)

    s = Script("arrival-reject", "A claimed arrival is rejected with 486: the leg ends, then the session", ["coverage"])
    s.watch("r1").inject("arrival", **arrival_args()).deliver("r1", 1, START, arrived(), 0)
    s.inject("owner_claim", **owner_rec(E1))
    s.call("r1", "RejectLeg", {"owner": owner(E1, acked=1), "command_id": s.next_cmd(), "leg_id": C, "sip_status": 486}, ok={})
    s.deliver("r1", 2, START, leg(C, "CUSTOMER", "ENDED", cause="DECLINED", sip=486, by="COMMAND"), E1)
    s.deliver("r1", 3, START, ended("ENDED_BY_OWNER", 486), E1)
    out.append(s)

    s = Script("decline-refused", "IE refuses an arrival without claiming it (its tenant check failed); the offered replica acknowledges the tombstone", ["coverage", "D5"])
    s.watch("r1").inject("arrival", **arrival_args()).deliver("r1", 1, START, arrived(), 0)
    s.call("r1", "DeclineArrival", {"tenant_id": T, "interaction_id": I, "replica_id": R1, "leg_id": C,
                                    "reason": "DECLINE_REASON_REFUSED", "sip_status": 403},
           ok={"rejected": True})
    s.deliver("r1", 2, START, leg(C, "CUSTOMER", "ENDED", cause="DECLINED", sip=403, by="COMMAND"), 0)
    s.deliver("r1", 3, START, ended("ARRIVAL_DECLINED", 403), 0)
    s.call("r1", "Acknowledge", {"acks": [owner(0, acked=3) | {"owner_epoch": "0"}]}, ok={}, tenant=None)
    out.append(s)

    s = Script("add-leg-agent-webrtc", "An agent's browser leg: originated, ringing, answered", ["coverage"])
    answered_call(s)
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), s.next_cmd()), ok={"state": "LEG_STATE_ORIGINATING"})
    s.inject("leg_ring", leg_id=A).deliver("r1", 3, START, leg(A, "AGENT", "RINGING"), E1)
    s.inject("leg_answer", leg_id=A).deliver("r1", 4, START, leg(A, "AGENT", "ANSWERED", answered=START), E1)
    out.append(s)

    s = Script("add-leg-outbound-pstn", "The first leg of an outbound session creates it (click-to-dial, a callback)", ["coverage"])
    s.watch("r1")
    s.inject("owner_claim", **owner_rec(E1, interaction=I2))
    s.call("r1", "AddLeg", add_leg(owner(E1, interaction=I2), s.next_cmd(), leg_id=P, role="CUSTOMER", endpoint="SIP",
                                   pstn={"number": OUT, "trunk_id": "carrier-a"}),
           ok={"state": "LEG_STATE_ORIGINATING"})
    s.inject("leg_ring", leg_id=P).deliver("r1", 1, START, leg(P, "CUSTOMER", "RINGING"), E1, interaction=I2)
    s.inject("leg_answer", leg_id=P).deliver("r1", 2, START, leg(P, "CUSTOMER", "ANSWERED", answered=START), E1, interaction=I2)
    get(s, "r1", E1, R1, "ACTIVE", "CALL", [snap_leg(P, "CUSTOMER", "ANSWERED", endpoint="SIP", answered=START)], 2,
        interaction=I2)
    out.append(s)

    s = Script("remove-leg", "An agent's leg removed; removing it again answers already_gone", ["coverage"])
    answered_call(s)
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), s.next_cmd()), ok={"state": "LEG_STATE_ORIGINATING"})
    s.inject("leg_answer", leg_id=A).deliver("r1", 3, START, leg(A, "AGENT", "ANSWERED", answered=START), E1)
    s.call("r1", "RemoveLeg", {"owner": owner(E1, acked=3), "command_id": s.next_cmd(), "leg_id": A, "cause": "END_CAUSE_NORMAL"}, ok={})
    s.deliver("r1", 4, START, leg(A, "AGENT", "ENDED", cause="NORMAL", by="COMMAND", answered=START), E1)
    s.call("r1", "RemoveLeg", {"owner": owner(E1, acked=4), "command_id": s.next_cmd(), "leg_id": A, "cause": "END_CAUSE_NORMAL"},
           ok={"already_gone": True})
    out.append(s)

    s = Script("end-session", "EndSession hangs up every leg (the customer's last) and ends the session; again: already_gone", ["coverage"])
    answered_call(s)
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), s.next_cmd()), ok={"state": "LEG_STATE_ORIGINATING"})
    s.inject("leg_answer", leg_id=A).deliver("r1", 3, START, leg(A, "AGENT", "ANSWERED", answered=START), E1)
    s.call("r1", "EndSession", {"owner": owner(E1, acked=3), "command_id": s.next_cmd(), "cause": "END_CAUSE_NORMAL"}, ok={})
    s.deliver("r1", 4, START, leg(A, "AGENT", "ENDED", cause="NORMAL", by="COMMAND", answered=START), E1)
    s.deliver("r1", 5, START, leg(C, "CUSTOMER", "ENDED", cause="NORMAL", by="COMMAND", answered=START), E1)
    s.deliver("r1", 6, START, ended("ENDED_BY_OWNER"), E1)
    s.call("r1", "EndSession", {"owner": owner(E1, acked=6), "command_id": s.next_cmd(), "cause": "END_CAUSE_NORMAL"},
           ok={"already_gone": True})
    out.append(s)

    s = Script("acknowledge-batch", "A batch: the owner's ack counts, a stale epoch's and another incarnation's are ignored; nothing is redelivered", ["coverage"])
    answered_call(s)
    s.call("r1", "Acknowledge", {"acks": [owner(E1, acked=2), owner(E1 - 1, acked=2), owner(E1, acked=2, inc=INC2)]},
           ok={"ignored": 2}, tenant=None)
    s.advance(2000).heartbeat("r1", START + 2000)
    out.append(s)

    # Races (21).
    order = rendezvous(I, [R1, R2])
    first, second = order[0], order[1]
    lab = {R1: "r1", R2: "r2"}
    s = Script("race-arrival-reoffer", f"X1: no ack within 1 s, so the arrival is offered to the next replica ({second}); the first ({first}) wins etcd and answers", ["X1"], streams=TWO)
    s.watch("r1").watch("r2").inject("arrival", **arrival_args())
    s.deliver(lab[first], 1, START, arrived(), 0)
    s.advance(1000).deliver(lab[second], 2, START + 1000, arrived(attempt=2), 0)
    s.inject("owner_claim", **owner_rec(E1, first))
    s.call(lab[first], "AnswerLeg", {"owner": owner(E1, first, acked=1), "command_id": s.next_cmd(first), "leg_id": C}, ok={})
    s.deliver(lab[first], 3, START + 1000, leg(C, "CUSTOMER", "ANSWERED", answered=START + 1000), E1)
    get(s, lab[first], E1, first, "ACTIVE", "CALL", [customer(answered=START + 1000)], 3)
    out.append(s)

    s = Script("race-arrival-deadline", "X2: IE acked the offer but its claim came after the 5 s deadline: 503, and the late command gets session_ended", ["X2"])
    s.watch("r1").inject("arrival", **arrival_args()).deliver("r1", 1, START, arrived(), 0)
    s.call("r1", "Acknowledge", {"acks": [owner(0, acked=1) | {"owner_epoch": "0"}]}, ok={}, tenant=None)
    s.advance(5000)
    s.deliver("r1", 2, START + 5000, leg(C, "CUSTOMER", "ENDED", cause="SYSTEM", sip=503, by="SYSTEM"), 0)
    s.deliver("r1", 3, START + 5000, ended("ARRIVAL_UNCLAIMED", 503), 0)
    s.inject("owner_claim", **owner_rec(E1))
    s.call("r1", "AnswerLeg", {"owner": owner(E1, acked=3), "command_id": s.next_cmd(), "leg_id": C},
           err=("FAILED_PRECONDITION", "session_ended"))
    get(s, "r1", E1, R1, "ENDED", "NONE",
        [customer("ENDED", cause="SYSTEM", sip=503, by="SYSTEM", ended_at=START + 5000)], 3,
        end_reason="ARRIVAL_UNCLAIMED", ended_at=START + 5000)
    out.append(s)

    s = Script("race-cancel-before-claim", "X3: the caller hangs up (CANCEL) before any claim; a late AnswerLeg gets session_ended", ["X3"])
    s.watch("r1").inject("arrival", **arrival_args()).deliver("r1", 1, START, arrived(), 0)
    s.inject("caller_cancel", leg_id=C)
    s.deliver("r1", 2, START, leg(C, "CUSTOMER", "ENDED", cause="CANCELED", sip=487, by="REMOTE"), 0)
    s.deliver("r1", 3, START, ended("ALL_LEGS_GONE"), 0)
    s.inject("owner_claim", **owner_rec(E1))
    s.call("r1", "AnswerLeg", {"owner": owner(E1, acked=3), "command_id": s.next_cmd(), "leg_id": C},
           err=("FAILED_PRECONDITION", "session_ended"))
    get(s, "r1", E1, R1, "ENDED", "NONE",
        [customer("ENDED", cause="CANCELED", sip=487, by="REMOTE", ended_at=START)], 3,
        end_reason="ALL_LEGS_GONE", ended_at=START)
    out.append(s)

    s = Script("race-decline-after-claim", "X5: a claimed arrival is closed: a decline gets arrival_closed and changes nothing", ["X5"])
    answered_call(s)
    s.call("r1", "DeclineArrival", {"tenant_id": T, "interaction_id": I, "replica_id": R1, "leg_id": C,
                                    "reason": "DECLINE_REASON_REFUSED", "sip_status": 503},
           err=("FAILED_PRECONDITION", "arrival_closed"))
    get(s, "r1", E1, R1, "ACTIVE", "CALL", [customer()], 2)
    out.append(s)

    s = Script("race-duplicate-add-leg", "X6: a retry (same command_id) gets the first answer; the same leg under a new command_id is existing; other parameters are leg_conflict; one originate", ["X6"])
    answered_call(s)
    c3 = s.next_cmd()
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), c3), ok={"state": "LEG_STATE_ORIGINATING"})
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), c3), ok={"state": "LEG_STATE_ORIGINATING"}).note("the same command_id: the first answer")
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), s.next_cmd()), ok={"existing": True, "state": "LEG_STATE_ORIGINATING"})
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), s.next_cmd(), contact="sip:agent-0b2c@acme.agents.example"),
           err=("ALREADY_EXISTS", "leg_conflict"))
    get(s, "r1", E1, R1, "ACTIVE", "CALL", [customer(), agent()], 2)
    out.append(s)

    s = Script("tc-restart-resync", "X7: TC restarts (a new incarnation); commands get rebuilding; IE resyncs with GetSession and retries", ["X7"])
    answered_call(s)
    c3 = s.next_cmd()
    s.inject("tc_restart", incarnation=INC2, rebuild_ms="500")
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), c3), err=("UNAVAILABLE", "rebuilding"))
    s.advance(500).watch("r1", at=START + 500, sessions=1, free=FREE - 1, inc=INC2)
    get(s, "r1", E1, R1, "ACTIVE", "CALL", [customer()], 0)
    s.call("r1", "AddLeg", add_leg(owner(E1), c3), ok={"state": "LEG_STATE_ORIGINATING"})
    get(s, "r1", E1, R1, "ACTIVE", "CALL", [customer(), agent()], 0)
    out.append(s)

    s = Script("race-adoption-replay", "X8: R1 dies with an event unacknowledged; R2 adopts (a higher epoch) and gets it replayed, identical", ["X8"], streams=TWO)
    s.watch("r2")
    answered_call(s)
    s.inject("stream_drop", stream="r1").inject("owner_delete", interaction_id=I).inject("owner_claim", **owner_rec(E2, R2))
    s.call("r2", "ClaimSession", {"owner": owner(E2, R2)},
           ok={"incarnation": s.inc, "session": session(E2, R2, "ACTIVE", "CALL", [customer()], 2)})
    s.deliver("r2", 2, START, leg(C, "CUSTOMER", "ANSWERED", answered=START), E1).note("the replay: the same message, epoch and all")
    get(s, "r2", E2, R2, "ACTIVE", "CALL", [customer()], 2)
    out.append(s)

    s = Script("race-adoption-during-add-leg", "X9: R1's AddLeg completes, R2 adopts; R1's retry gets stale_owner (fence before de-dup); the leg's answer goes to R2", ["X9"], streams=TWO)
    s.watch("r2")
    answered_call(s)
    c3 = s.next_cmd()
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), c3), ok={"state": "LEG_STATE_ORIGINATING"})
    s.inject("owner_delete", interaction_id=I).inject("owner_claim", **owner_rec(E2, R2))
    s.call("r2", "ClaimSession", {"owner": owner(E2, R2)},
           ok={"incarnation": s.inc, "session": session(E2, R2, "ACTIVE", "CALL", [customer(), agent()], 2)})
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), c3), err=("ABORTED", "stale_owner"))
    s.inject("leg_answer", leg_id=A).deliver("r2", 3, START, leg(A, "AGENT", "ANSWERED", answered=START), E2)
    get(s, "r2", E2, R2, "ACTIVE", "CALL", [customer(), agent(state="ANSWERED", answered=START)], 3)
    out.append(s)

    s = Script("race-reinvite-during-adoption", "X10: a carrier's re-INVITE moves its media while no owner is alive: TC follows it on the node; the adopter gets the event replayed", ["X10"], streams=TWO)
    answered_call(s)
    s.inject("stream_drop", stream="r1").inject("reinvite_address", leg_id=C)
    s.inject("owner_delete", interaction_id=I).inject("owner_claim", **owner_rec(E2, R2))
    s.watch("r2", sessions=1, free=FREE - 1)
    s.call("r2", "ClaimSession", {"owner": owner(E2, R2)},
           ok={"incarnation": s.inc, "session": session(E2, R2, "ACTIVE", "CALL", [customer()], 3)})
    s.deliver("r2", 2, START, leg(C, "CUSTOMER", "ANSWERED", answered=START), E1)
    s.deliver("r2", 3, START, {"remote_media_changed": {"leg_id": C, "kind": "REMOTE_MEDIA_KIND_ADDRESS"}}, E1)
    get(s, "r2", E2, R2, "ACTIVE", "CALL", [customer()], 3)
    out.append(s)

    s = Script("race-hangup-during-claim-a", "X11 (hangup first): the claim returns the ended session's tombstone and the end is replayed", ["X11"], streams=TWO)
    answered_call(s)
    s.inject("stream_drop", stream="r1").inject("leg_hangup", leg_id=C)
    s.inject("owner_delete", interaction_id=I).inject("owner_claim", **owner_rec(E2, R2))
    s.watch("r2")
    tomb = [customer("ENDED", cause="NORMAL", by="REMOTE", answered=START, ended_at=START)]
    s.call("r2", "ClaimSession", {"owner": owner(E2, R2)},
           ok={"incarnation": s.inc, "session": session(E2, R2, "ENDED", "NONE", tomb, 4, end_reason="ALL_LEGS_GONE", ended_at=START)})
    s.deliver("r2", 2, START, leg(C, "CUSTOMER", "ANSWERED", answered=START), E1)
    s.deliver("r2", 3, START, leg(C, "CUSTOMER", "ENDED", cause="NORMAL", by="REMOTE", answered=START), E1)
    s.deliver("r2", 4, START, ended("ALL_LEGS_GONE"), E1)
    s.call("r2", "Acknowledge", {"acks": [owner(E2, R2, acked=4)]}, ok={}, tenant=None)
    get(s, "r2", E2, R2, "ENDED", "NONE", tomb, 4, end_reason="ALL_LEGS_GONE", ended_at=START)
    out.append(s)

    s = Script("race-hangup-during-claim-b", "X11 (claim first): the claim sees the call alive; the hangup's events follow on the adopter's stream", ["X11"], streams=TWO)
    answered_call(s)
    s.inject("stream_drop", stream="r1")
    s.inject("owner_delete", interaction_id=I).inject("owner_claim", **owner_rec(E2, R2))
    s.watch("r2", sessions=1, free=FREE - 1)
    s.call("r2", "ClaimSession", {"owner": owner(E2, R2)},
           ok={"incarnation": s.inc, "session": session(E2, R2, "ACTIVE", "CALL", [customer()], 2)})
    s.deliver("r2", 2, START, leg(C, "CUSTOMER", "ANSWERED", answered=START), E1)
    s.inject("leg_hangup", leg_id=C)
    s.deliver("r2", 3, START, leg(C, "CUSTOMER", "ENDED", cause="NORMAL", by="REMOTE", answered=START), E2)
    s.deliver("r2", 4, START, ended("ALL_LEGS_GONE"), E2)
    get(s, "r2", E2, R2, "ENDED", "NONE", [customer("ENDED", cause="NORMAL", by="REMOTE", answered=START, ended_at=START)], 4,
        end_reason="ALL_LEGS_GONE", ended_at=START)
    out.append(s)

    s = Script("race-adopt-ended-session", "Adopting a session that already ended: R1 ended it and died before acknowledging; a minute later R2 claims it, gets the ENDED tombstone and the end replayed, and its ack releases the tombstone 10 minutes later",
               ["X19", "IE ADR 0003"], streams=TWO)
    answered_call(s)
    s.call("r1", "EndSession", {"owner": owner(E1, acked=2), "command_id": s.next_cmd(), "cause": "END_CAUSE_NORMAL"}, ok={})
    s.deliver("r1", 3, START, leg(C, "CUSTOMER", "ENDED", cause="NORMAL", by="COMMAND", answered=START), E1)
    s.deliver("r1", 4, START, ended("ENDED_BY_OWNER"), E1)
    s.inject("stream_drop", stream="r1").advance(60000)
    s.inject("owner_delete", interaction_id=I).inject("owner_claim", **owner_rec(E2, R2))
    s.watch("r2", at=START + 60000)
    tomb = [customer("ENDED", cause="NORMAL", by="COMMAND", answered=START, ended_at=START)]
    s.call("r2", "ClaimSession", {"owner": owner(E2, R2)},
           ok={"incarnation": s.inc, "session": session(E2, R2, "ENDED", "NONE", tomb, 4, end_reason="ENDED_BY_OWNER", ended_at=START)})
    s.deliver("r2", 3, START, leg(C, "CUSTOMER", "ENDED", cause="NORMAL", by="COMMAND", answered=START), E1)
    s.deliver("r2", 4, START, ended("ENDED_BY_OWNER"), E1)
    s.call("r2", "Acknowledge", {"acks": [owner(E2, R2, acked=4)]}, ok={}, tenant=None).note("after R2's ENDED checkpoint")
    s.advance(599000)
    get(s, "r2", E2, R2, "ENDED", "NONE", tomb, 4, end_reason="ENDED_BY_OWNER", ended_at=START)
    s.advance(1000)
    s.call("r2", "GetSession", {"tenant_id": T, "interaction_id": I, "replica_id": R2}, err=("NOT_FOUND", "session_unknown"))
    out.append(s)

    s = Script("race-partition-handover", "X12: R1 is cut off from TC; after 10 s it hands over, R2 adopts; R1's late command gets stale_owner and its new stream gets nothing for the session",
               ["X12"], streams={**TWO, "r1b": {"replica_id": R1, "bin": "bin-a"}})
    answered_call(s)
    s.inject("stream_drop", stream="r1").advance(10000)
    s.inject("owner_delete", interaction_id=I).inject("owner_claim", **owner_rec(E2, R2))
    s.watch("r2", at=START + 10000, sessions=1, free=FREE - 1)
    s.call("r2", "ClaimSession", {"owner": owner(E2, R2)},
           ok={"incarnation": s.inc, "session": session(E2, R2, "ACTIVE", "CALL", [customer()], 2)})
    s.deliver("r2", 2, START, leg(C, "CUSTOMER", "ANSWERED", answered=START), E1)
    s.watch("r1b", at=START + 10000, sessions=1, free=FREE - 1).note("the partition heals")
    s.call("r1b", "AddLeg", add_leg(owner(E1, acked=2), s.next_cmd(), leg_id=A2), err=("ABORTED", "stale_owner"))
    s.call("r2", "AddLeg", add_leg(owner(E2, R2, acked=2), s.next_cmd(R2)), ok={"state": "LEG_STATE_ORIGINATING"})
    get(s, "r2", E2, R2, "ACTIVE", "CALL", [customer(), agent()], 2)
    out.append(s)

    s = Script("race-stream-superseded", "X13: a second stream of one replica supersedes the first (ABORTED / stream_superseded, asserted by the runner); unacknowledged events are replayed on it",
               ["X13"], streams={"r1": {"replica_id": R1, "bin": "bin-a"}, "r1b": {"replica_id": R1, "bin": "bin-a"}})
    s.watch("r1").inject("arrival", **arrival_args()).deliver("r1", 1, START, arrived(), 0)
    s.inject("owner_claim", **owner_rec(E1))
    s.call("r1", "AnswerLeg", {"owner": owner(E1), "command_id": s.next_cmd(), "leg_id": C}, ok={})
    s.deliver("r1", 2, START, leg(C, "CUSTOMER", "ANSWERED", answered=START), E1)
    s.watch("r1b", sessions=1, free=FREE - 1)
    s.deliver("r1b", 2, START, leg(C, "CUSTOMER", "ANSWERED", answered=START), E1)
    s.call("r1b", "Acknowledge", {"acks": [owner(E1, acked=2)]}, ok={}, tenant=None)
    get(s, "r1b", E1, R1, "ACTIVE", "CALL", [customer()], 2)
    out.append(s)

    s = Script("race-replay-after-reconnect", "X13: a dropped stream reconnects: the unacknowledged event is replayed, then redelivered every 2 s until acknowledged", ["X13"])
    answered_call(s)
    s.inject("stream_drop", stream="r1").watch("r1", sessions=1, free=FREE - 1)
    s.deliver("r1", 2, START, leg(C, "CUSTOMER", "ANSWERED", answered=START), E1).note("the replay on reconnect")
    s.advance(2000).deliver("r1", 2, START, leg(C, "CUSTOMER", "ANSWERED", answered=START), E1).note("redelivered after 2 s")
    s.call("r1", "Acknowledge", {"acks": [owner(E1, acked=2)]}, ok={}, tenant=None)
    s.advance(2000)
    get(s, "r1", E1, R1, "ACTIVE", "CALL", [customer()], 2)
    out.append(s)

    s = Script("race-ack-old-incarnation", "X14: after a restart, an ack naming the old incarnation is ignored (the event is redelivered); one naming the new counts", ["X14"])
    answered_call(s)
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), s.next_cmd()), ok={"state": "LEG_STATE_ORIGINATING"})
    s.inject("tc_restart", incarnation=INC2, rebuild_ms="0")
    s.watch("r1", sessions=1, free=FREE - 2, inc=INC2)
    s.inject("leg_answer", leg_id=A).deliver("r1", 1, START, leg(A, "AGENT", "ANSWERED", answered=START), E1, inc=INC2)
    s.call("r1", "Acknowledge", {"acks": [owner(E1, acked=1, inc=INC1)]}, ok={"ignored": 1}, tenant=None)
    s.advance(2000).deliver("r1", 1, START, leg(A, "AGENT", "ANSWERED", answered=START), E1, inc=INC2)
    s.call("r1", "Acknowledge", {"acks": [owner(E1, acked=1, inc=INC2)]}, ok={}, tenant=None)
    get(s, "r1", E1, R1, "ACTIVE", "CALL", [customer(), agent(state="ANSWERED", answered=START)], 1)
    out.append(s)

    s = Script("race-remove-vs-answer", "X17: IE removes the agent's leg as it answers (IE hadn't seen the answer): ANSWERED, then ENDED by COMMAND", ["X17"])
    answered_call(s)
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), s.next_cmd()), ok={"state": "LEG_STATE_ORIGINATING"})
    s.inject("leg_ring", leg_id=A).deliver("r1", 3, START, leg(A, "AGENT", "RINGING"), E1)
    s.inject("leg_answer", leg_id=A).deliver("r1", 4, START, leg(A, "AGENT", "ANSWERED", answered=START), E1)
    s.call("r1", "RemoveLeg", {"owner": owner(E1, acked=3), "command_id": s.next_cmd(), "leg_id": A, "cause": "END_CAUSE_NORMAL"}, ok={})
    s.deliver("r1", 5, START, leg(A, "AGENT", "ENDED", cause="NORMAL", by="COMMAND", answered=START), E1)
    get(s, "r1", E1, R1, "ACTIVE", "CALL",
        [customer(), agent(state="ENDED", cause="NORMAL", by="COMMAND", answered=START, ended_at=START)], 5)
    out.append(s)

    s = Script("race-caller-gone-while-ringing", "X18: the customer hangs up while the agent's leg rings: freeswitch's hook cancels it, then the session ends", ["X18"])
    answered_call(s)
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), s.next_cmd()), ok={"state": "LEG_STATE_ORIGINATING"})
    s.inject("leg_ring", leg_id=A).deliver("r1", 3, START, leg(A, "AGENT", "RINGING"), E1)
    s.inject("leg_hangup", leg_id=C)
    s.deliver("r1", 4, START, leg(C, "CUSTOMER", "ENDED", cause="NORMAL", by="REMOTE", answered=START), E1)
    s.deliver("r1", 5, START, leg(A, "AGENT", "ENDED", cause="CANCELED", by="SYSTEM"), E1)
    s.deliver("r1", 6, START, ended("ALL_LEGS_GONE"), E1)
    get(s, "r1", E1, R1, "ENDED", "NONE",
        [customer("ENDED", cause="NORMAL", by="REMOTE", answered=START, ended_at=START),
         agent(state="ENDED", cause="CANCELED", by="SYSTEM", ended_at=START)], 6, end_reason="ALL_LEGS_GONE", ended_at=START)
    out.append(s)

    s = Script("tombstone-ack", "X19: the tombstone outlives the owner's ack by 10 minutes, then the session is unknown", ["X19"])
    answered_call(s)
    s.call("r1", "EndSession", {"owner": owner(E1, acked=2), "command_id": s.next_cmd(), "cause": "END_CAUSE_NORMAL"}, ok={})
    s.deliver("r1", 3, START, leg(C, "CUSTOMER", "ENDED", cause="NORMAL", by="COMMAND", answered=START), E1)
    s.deliver("r1", 4, START, ended("ENDED_BY_OWNER"), E1)
    s.call("r1", "Acknowledge", {"acks": [owner(E1, acked=4)]}, ok={}, tenant=None).note("after IE's ENDED checkpoint is durable")
    tomb = [customer("ENDED", cause="NORMAL", by="COMMAND", answered=START, ended_at=START)]
    s.advance(599000)
    get(s, "r1", E1, R1, "ENDED", "NONE", tomb, 4, end_reason="ENDED_BY_OWNER", ended_at=START)
    s.advance(1000)
    s.call("r1", "GetSession", {"tenant_id": T, "interaction_id": I, "replica_id": R1}, err=("NOT_FOUND", "session_unknown"))
    out.append(s)

    s = Script("session-lost", "X20: freeswitch dies: every leg ends and the session ends FREESWITCH_LOST", ["X20"])
    answered_call(s)
    s.inject("freeswitch_lost")
    s.deliver("r1", 3, START, leg(C, "CUSTOMER", "ENDED", cause="SYSTEM", by="SYSTEM", answered=START), E1)
    s.deliver("r1", 4, START, ended("FREESWITCH_LOST"), E1)
    get(s, "r1", E1, R1, "ENDED", "NONE", [customer("ENDED", cause="SYSTEM", by="SYSTEM", answered=START, ended_at=START)], 4,
        end_reason="FREESWITCH_LOST", ended_at=START)
    out.append(s)

    s = Script("race-add-leg-retry-after-restart", "X21 (R1-1): the agent's leg failed, TC restarted, IE retries the AddLeg: existing, ENDED, BUSY; never originated again (the runners check the node)", ["X21", "R1-1"])
    answered_call(s)
    c3 = s.next_cmd()
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), c3), ok={"state": "LEG_STATE_ORIGINATING"})
    s.inject("leg_fail", leg_id=A, cause="END_CAUSE_BUSY", sip_status=486)
    s.deliver("r1", 3, START, leg(A, "AGENT", "ENDED", cause="BUSY", sip=486, by="REMOTE"), E1)
    s.inject("tc_restart", incarnation=INC2, rebuild_ms="0")
    s.watch("r1", sessions=1, free=FREE - 1, inc=INC2)
    s.call("r1", "AddLeg", add_leg(owner(E1), c3),
           ok={"existing": True, "state": "LEG_STATE_ENDED", "cause": "END_CAUSE_BUSY"})
    get(s, "r1", E1, R1, "ACTIVE", "CALL",
        [customer(), agent(state="ENDED", cause="BUSY", sip=486, by="REMOTE", ended_at=START)], 0)
    out.append(s)

    s = Script("race-claim-during-tc-restart", "X22 (R1-3): TC dies after raising R2's epoch on one leg of two; the rebuild takes the highest cc_owner_epoch; R2's claim gets rebuilding, then existing, and completes the raise (the runners check every leg and the media)",
               ["X22", "R1-3"], streams=TWO)
    answered_call(s)
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), s.next_cmd()), ok={"state": "LEG_STATE_ORIGINATING"})
    s.inject("leg_answer", leg_id=A).deliver("r1", 3, START, leg(A, "AGENT", "ANSWERED", answered=START), E1)
    s.inject("stream_drop", stream="r1")
    s.inject("owner_delete", interaction_id=I).inject("owner_claim", **owner_rec(E2, R2))
    s.inject("tc_crash_mid_fence_raise", claim=owner(E2, R2), after_legs=1, incarnation=INC2, rebuild_ms="500")
    s.call("r2", "ClaimSession", {"owner": owner(E2, R2)}, err=("UNAVAILABLE", "rebuilding"))
    s.advance(500).watch("r2", at=START + 500, sessions=1, free=FREE - 2, inc=INC2)
    both = [customer(), agent(state="ANSWERED", answered=START)]
    s.call("r2", "ClaimSession", {"owner": owner(E2, R2)}, ok={"incarnation": s.inc, "session": session(E2, R2, "ACTIVE", "CALL", both, 0),
                                                               "existing": True})
    s.call("r2", "RemoveLeg", {"owner": owner(E1), "command_id": s.next_cmd(), "leg_id": A, "cause": "END_CAUSE_NORMAL"},
           err=("ABORTED", "stale_owner")).note("R1's late command, from bin-a too")
    get(s, "r2", E2, R2, "ACTIVE", "CALL", both, 0)
    out.append(s)

    # Errors (11).
    s = Script("owner-unverified", "etcd names another replica for this epoch: PERMISSION_DENIED / owner_unverified", ["errors"])
    s.watch("r1").inject("arrival", **arrival_args()).deliver("r1", 1, START, arrived(), 0)
    s.inject("owner_claim", **owner_rec(E1, R2))
    s.call("r1", "AnswerLeg", {"owner": owner(E1, acked=1), "command_id": s.next_cmd(), "leg_id": C},
           err=("PERMISSION_DENIED", "owner_unverified"))
    out.append(s)

    s = Script("owner-check-unavailable", "A new epoch needs etcd, which is down: UNAVAILABLE / owner_check_unavailable (fail closed); the retry succeeds", ["errors"])
    s.watch("r1").inject("arrival", **arrival_args()).deliver("r1", 1, START, arrived(), 0)
    s.inject("owner_claim", **owner_rec(E1)).inject("etcd_down")
    c = s.next_cmd()
    s.call("r1", "AnswerLeg", {"owner": owner(E1, acked=1), "command_id": c, "leg_id": C},
           err=("UNAVAILABLE", "owner_check_unavailable"))
    s.inject("etcd_up")
    s.call("r1", "AnswerLeg", {"owner": owner(E1, acked=1), "command_id": c, "leg_id": C}, ok={})
    s.deliver("r1", 2, START, leg(C, "CUSTOMER", "ANSWERED", answered=START), E1)
    out.append(s)

    s = Script("identity-mismatch", "A replica of bin-b opens a stream for bin-a: PERMISSION_DENIED / identity_mismatch", ["errors"],
               peers={"bin-b": "parity-interaction-engine.bin-b"})
    s.watch("r1", peer="bin-b", error=("PERMISSION_DENIED", "identity_mismatch"))
    out.append(s)

    s = Script("foreign-session", "Another tenant's interaction: NOT_FOUND / session_unknown, never a confirmation it exists", ["errors"])
    answered_call(s)
    s.call("r1", "GetSession", {"tenant_id": T2, "interaction_id": I, "replica_id": R1}, err=("NOT_FOUND", "session_unknown"), tenant=T2)
    out.append(s)

    s = Script("leg-unknown", "A leg the session doesn't have: NOT_FOUND / leg_unknown", ["errors"])
    answered_call(s)
    s.call("r1", "RemoveLeg", {"owner": owner(E1, acked=2), "command_id": s.next_cmd(), "leg_id": A2, "cause": "END_CAUSE_NORMAL"},
           err=("NOT_FOUND", "leg_unknown"))
    out.append(s)

    s = Script("command-conflict", "A command_id reused for another request: ALREADY_EXISTS / command_conflict", ["errors"])
    answered_call(s)
    c = s.next_cmd()
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), c), ok={"state": "LEG_STATE_ORIGINATING"})
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), c, leg_id=A2), err=("ALREADY_EXISTS", "command_conflict"))
    out.append(s)

    s = Script("wrong-node", "The owner record names another media node: FAILED_PRECONDITION / wrong_node", ["errors"])
    s.watch("r1").inject("owner_claim", **owner_rec(E1, interaction=I2, node="media-2"))
    s.call("r1", "AddLeg", add_leg(owner(E1, interaction=I2), s.next_cmd(), leg_id=P, role="CUSTOMER", endpoint="SIP",
                                   pstn={"number": OUT, "trunk_id": "carrier-a"}),
           err=("FAILED_PRECONDITION", "wrong_node"))
    out.append(s)

    s = Script("invalid-state", "An agent's leg before the customer's is answered: FAILED_PRECONDITION / invalid_state", ["errors"])
    s.watch("r1").inject("arrival", **arrival_args()).deliver("r1", 1, START, arrived(), 0)
    s.inject("owner_claim", **owner_rec(E1))
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=1), s.next_cmd()), err=("FAILED_PRECONDITION", "invalid_state"))
    out.append(s)

    s = Script("no-capacity", "No parity-ms legs left: RESOURCE_EXHAUSTED / no_capacity", ["errors"])
    answered_call(s)
    s.inject("capacity", free_legs=0)
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), s.next_cmd()), err=("RESOURCE_EXHAUSTED", "no_capacity"))
    out.append(s)

    s = Script("draining", "A draining node refuses a new outbound session (UNAVAILABLE / draining) and keeps serving the calls it has (F3)", ["errors", "F3"])
    answered_call(s)
    s.inject("drain").inject("owner_claim", **owner_rec(E3, interaction=I2))
    s.call("r1", "AddLeg", add_leg(owner(E3, interaction=I2), s.next_cmd(), leg_id=P, role="CUSTOMER", endpoint="SIP",
                                   pstn={"number": OUT, "trunk_id": "carrier-a"}),
           err=("UNAVAILABLE", "draining"))
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), s.next_cmd()), ok={"state": "LEG_STATE_ORIGINATING"})
    out.append(s)

    s = Script("leg-id-across-sessions", "A leg id names one leg on the node: another tenant's session, or another session of the same tenant, gets leg_conflict, creates nothing, and the leg's events stay with its own session", ["errors", "fix 1"])
    answered_call(s)
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), s.next_cmd()), ok={"state": "LEG_STATE_ORIGINATING"})
    s.inject("owner_claim", **owner_rec(E3, interaction=I2, tenant=T2))
    s.call("r1", "AddLeg", add_leg(owner(E3, interaction=I2, tenant=T2), s.next_cmd(), leg_id=A, role="CUSTOMER",
                                   endpoint="SIP", pstn={"number": OUT, "trunk_id": "carrier-a"}),
           err=("ALREADY_EXISTS", "leg_conflict"), tenant=T2)
    s.call("r1", "GetSession", {"tenant_id": T2, "interaction_id": I2, "replica_id": R1}, err=("NOT_FOUND", "session_unknown"),
           tenant=T2).note("no session was left behind")
    s.inject("owner_claim", **owner_rec(E4, interaction=I3))
    s.call("r1", "AddLeg", add_leg(owner(E4, interaction=I3), s.next_cmd(), leg_id=A), err=("ALREADY_EXISTS", "leg_conflict"))
    s.inject("leg_answer", leg_id=A).deliver("r1", 3, START, leg(A, "AGENT", "ANSWERED", answered=START), E1)
    get(s, "r1", E1, R1, "ACTIVE", "CALL", [customer(), agent(state="ANSWERED", answered=START)], 3)
    out.append(s)

    s = Script("race-expired-attempt", "R1-8 (TC ADR 0002): an AddLeg attempt that arrives after its expiry (parity-attempt-expires-ms, less TC's 250 ms skew budget) does nothing: DEADLINE_EXCEEDED / attempt_expired, the leg is absent; the retry with a fresh expiry and the same command_id originates it once (the runners check the node)", ["R1-8", "errors"])
    answered_call(s)
    cmd = s.next_cmd()
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), cmd), err=("DEADLINE_EXCEEDED", "attempt_expired"),
           metadata_extra={"parity-attempt-expires-ms": s64(START + 100)})
    get(s, "r1", E1, R1, "ACTIVE", "CALL", [customer()], 2)
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), cmd), ok={"state": "LEG_STATE_ORIGINATING"},
           metadata_extra={"parity-attempt-expires-ms": s64(START + 1000)})
    s.inject("leg_ring", leg_id=A).deliver("r1", 3, START, leg(A, "AGENT", "RINGING"), E1)
    get(s, "r1", E1, R1, "ACTIVE", "CALL", [customer(), agent(state="RINGING")], 3)
    out.append(s)

    s = Script("session-busy", "R1-5 (TC ADR 0002): a session whose mailbox is full refuses a command with UNAVAILABLE / session_busy and does nothing; the retry with the same command_id succeeds", ["R1-5", "errors"])
    s.watch("r1").inject("arrival", **arrival_args()).deliver("r1", 1, START, arrived(), 0)
    s.inject("owner_claim", **owner_rec(E1))
    s.inject("mailbox_full", tenant_id=T, interaction_id=I)
    cmd = s.next_cmd()
    s.call("r1", "AnswerLeg", {"owner": owner(E1, acked=1), "command_id": cmd, "leg_id": C},
           err=("UNAVAILABLE", "session_busy"))
    s.inject("mailbox_free", tenant_id=T, interaction_id=I)
    s.call("r1", "AnswerLeg", {"owner": owner(E1, acked=1), "command_id": cmd, "leg_id": C}, ok={})
    s.deliver("r1", 2, START, leg(C, "CUSTOMER", "ANSWERED", answered=START), E1)
    out.append(s)

    s = Script("dependency-unavailable", "freeswitch (ESL), then parity-ms, can't be reached: UNAVAILABLE with each code; the retry succeeds", ["errors"])
    answered_call(s)
    c = s.next_cmd()
    s.inject("freeswitch_unreachable")
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), c), err=("UNAVAILABLE", "freeswitch_unavailable"))
    s.inject("freeswitch_reachable").inject("parity_ms_unreachable")
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), c), err=("UNAVAILABLE", "parity_ms_unavailable"))
    s.inject("parity_ms_reachable")
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), c), ok={"state": "LEG_STATE_ORIGINATING"})
    out.append(s)

    # Writer-invalid (6): the last call breaks the listed rules; the expected error follows from them.
    # Step 4: a leg's media through parity-ms (TC ADR 0003): one script per command and event.
    PL1, PL2 = "0192f6a1-9000-7000-8000-000000000001", "0192f6a1-9000-7000-8000-000000000002"
    COLL = "0192f6a1-a000-7000-8000-000000000001"

    def media_call(s, seq, rpc, body, ok=None, err=None, acked=2):
        b = {"owner": owner(E1, acked=acked), "command_id": s.next_cmd()}
        b.update(body)
        return s.call("r1", rpc, b, ok=ok, err=err)

    s = Script("play-prompt", "A tenant prompt played into the customer's leg; Play is repeat-safe by play_id; parity-ms's end becomes PlayFinished", ["coverage"])
    answered_call(s)
    media_call(s, 2, "Play", {"leg_id": C, "play_id": PL1, "prompt_id": "welcome"}, ok={})
    media_call(s, 2, "Play", {"leg_id": C, "play_id": PL1, "prompt_id": "welcome"}, ok={"existing": True})
    s.inject("play_done", leg_id=C, reason="completed", played_ms=2000)
    s.deliver("r1", 3, START, {"play_finished": {"leg_id": C, "play_id": PL1, "reason": "PLAY_END_REASON_COMPLETED",
                                                 "played_ms": 2000}}, E1)
    out.append(s)

    s = Script("play-loop-stop", "A looped prompt plays again when it completes; StopPlay ends it (STOPPED); again: already_gone", ["coverage"])
    answered_call(s)
    media_call(s, 2, "Play", {"leg_id": C, "play_id": PL2, "prompt_id": "music", "loop": True}, ok={})
    s.inject("play_done", leg_id=C, reason="completed", played_ms=30000).note("a loop: no event, it plays again")
    media_call(s, 2, "StopPlay", {"leg_id": C, "play_id": PL2}, ok={})
    s.inject("play_done", leg_id=C, reason="stopped", played_ms=1200)
    s.deliver("r1", 3, START, {"play_finished": {"leg_id": C, "play_id": PL2, "reason": "PLAY_END_REASON_STOPPED",
                                                 "played_ms": 1200}}, E1)
    media_call(s, 3, "StopPlay", {"leg_id": C, "play_id": PL2}, ok={"already_gone": True}, acked=3)
    out.append(s)

    s = Script("prompt-unknown", "A prompt parity-ms doesn't have: prompt_unknown, and nothing plays", ["coverage"])
    answered_call(s)
    s.inject("prompt_missing", prompt_id="nosuch")
    media_call(s, 2, "Play", {"leg_id": C, "play_id": PL1, "prompt_id": "nosuch"}, err=("FAILED_PRECONDITION", "prompt_unknown"))
    out.append(s)

    s = Script("play-result-events", "Play with result_events (0.17.0, the play saga): the answer means TC took it; PlayStarted, then PlayFinished; StopPlay before the end is STOPPED; a prompt parity-ms doesn't have: PlayFailed(PROMPT_UNKNOWN), and the call goes on", ["coverage"])
    answered_call(s)
    media_call(s, 2, "Play", {"leg_id": C, "play_id": PL1, "prompt_id": "welcome", "result_events": True}, ok={})
    s.deliver("r1", 3, START, {"play_started": {"leg_id": C, "play_id": PL1}}, E1)
    media_call(s, 3, "Play", {"leg_id": C, "play_id": PL1, "prompt_id": "welcome", "result_events": True}, ok={"existing": True}, acked=3)
    media_call(s, 3, "StopPlay", {"leg_id": C, "play_id": PL1}, ok={}, acked=3)
    s.inject("play_done", leg_id=C, reason="stopped", played_ms=800)
    s.deliver("r1", 4, START, {"play_finished": {"leg_id": C, "play_id": PL1, "reason": "PLAY_END_REASON_STOPPED",
                                                 "played_ms": 800}}, E1)
    s.inject("prompt_missing", prompt_id="nosuch")
    media_call(s, 4, "Play", {"leg_id": C, "play_id": PL2, "prompt_id": "nosuch", "result_events": True}, ok={}, acked=4)
    s.deliver("r1", 5, START, {"play_failed": {"leg_id": C, "play_id": PL2, "reason": "PLAY_FAIL_REASON_PROMPT_UNKNOWN"}}, E1)
    out.append(s)

    s = Script("set-mute", "Mute silences what a leg sends; changed only when it changes", ["coverage"])
    answered_call(s)
    media_call(s, 2, "SetMute", {"leg_id": C, "muted": True}, ok={"changed": True})
    media_call(s, 2, "SetMute", {"leg_id": C, "muted": True}, ok={})
    media_call(s, 2, "SetMute", {"leg_id": C, "muted": False}, ok={"changed": True})
    out.append(s)

    s = Script("send-dtmf", "Digits sent to a leg's endpoint (RFC 4733 through parity-ms)", ["coverage"])
    answered_call(s)
    media_call(s, 2, "SendDtmf", {"leg_id": C, "digits": "12#"}, ok={})
    out.append(s)

    s = Script("digit-reporting", "Digits reach IE only under a collection: masked (no digit), then on; off again at its deadline", ["coverage", "PCI"])
    answered_call(s)
    s.inject("dtmf", leg_id=C, digit="5").note("reporting is off: nothing")
    media_call(s, 2, "SetDigitReporting", {"leg_id": C, "mode": "DIGIT_REPORTING_MASKED", "collection_id": COLL,
                                           "max_duration_s": 30}, ok={})
    s.inject("dtmf", leg_id=C, digit="5")
    s.deliver("r1", 3, START, {"dtmf": {"leg_id": C, "duration_ms": 100, "source": "DIGIT_SOURCE_RFC4733",
                                        "collection_id": COLL}}, E1)
    media_call(s, 3, "SetDigitReporting", {"leg_id": C, "mode": "DIGIT_REPORTING_ON", "collection_id": COLL,
                                           "max_duration_s": 30}, ok={}, acked=3)
    s.inject("dtmf", leg_id=C, digit="9", source="DIGIT_SOURCE_INBAND")
    s.deliver("r1", 4, START, {"dtmf": {"leg_id": C, "digit": "9", "duration_ms": 100, "source": "DIGIT_SOURCE_INBAND",
                                        "collection_id": COLL}}, E1)
    s.call("r1", "Acknowledge", {"acks": [owner(E1, acked=4)]}, ok={}, tenant=None)
    s.advance(31000)
    s.inject("dtmf", leg_id=C, digit="1").note("past the collection's deadline: off, nothing")
    out.append(s)

    s = Script("set-hold", "Hold mutes the parties toward each other and plays the music to the customer; changed only when it changes", ["coverage"])
    answered_call(s)
    media_call(s, 2, "SetHold", {"on": True, "music_prompt_id": "music"}, ok={"changed": True})
    media_call(s, 2, "SetHold", {"on": True, "music_prompt_id": "music"}, ok={})
    media_call(s, 2, "SetHold", {"on": False}, ok={"changed": True})
    out.append(s)

    s = Script("remote-hold", "The customer's own hold and resume by re-INVITE reach IE", ["coverage"])
    answered_call(s)
    s.inject("reinvite_hold", leg_id=C, on=True)
    s.deliver("r1", 3, START, {"remote_hold": {"leg_id": C, "on": True}}, E1)
    s.inject("reinvite_hold", leg_id=C, on=False)
    s.deliver("r1", 4, START, {"remote_hold": {"leg_id": C}}, E1)
    out.append(s)

    s = Script("media-activity", "Media stopping and coming back on a leg reach IE once per change", ["coverage"])
    answered_call(s)
    s.inject("media_inactive", leg_id=C)
    s.deliver("r1", 3, START, {"media_activity": {"leg_id": C}}, E1)
    s.inject("media_active", leg_id=C)
    s.deliver("r1", 4, START, {"media_activity": {"leg_id": C, "active": True}}, E1)
    s.inject("media_active", leg_id=C).note("no change: nothing")
    out.append(s)

    s = Script("dtmf-actions", "An agent's action code (P5): bound, matched by parity-ms, reported with the state after it; a customer can't bind one", ["coverage"])
    answered_call(s)
    s.call("r1", "AddLeg", add_leg(owner(E1, acked=2), s.next_cmd()), ok={"state": "LEG_STATE_ORIGINATING"})
    s.inject("leg_answer", leg_id=A).deliver("r1", 3, START, leg(A, "AGENT", "ANSWERED", answered=START), E1)
    media_call(s, 3, "SetDtmfActions", {"leg_id": A, "actions": {"bindings": [
        {"dtmf_sequence": "*1", "action": "DTMF_ACTION_KIND_MUTE_TOGGLE"}]}}, ok={}, acked=3)
    s.inject("dtmf_action_match", leg_id=A, action="DTMF_ACTION_KIND_MUTE_TOGGLE", result="DTMF_ACTION_RESULT_APPLIED")
    s.deliver("r1", 4, START, {"dtmf_action": {"leg_id": A, "role": "LEG_ROLE_AGENT", "action": "DTMF_ACTION_KIND_MUTE_TOGGLE",
                                               "result": "DTMF_ACTION_RESULT_APPLIED", "state": {"muted": True}}}, E1)
    media_call(s, 4, "SetDtmfActions", {"leg_id": C, "actions": {"bindings": [
        {"dtmf_sequence": "*2", "action": "DTMF_ACTION_KIND_MUTE"}]}}, err=("INVALID_ARGUMENT", "invalid_request"), acked=4)
    # 0.10.3 (media-r7-p5): every result, never the digits; a supervisor's mode code (IE step 14's shape).
    media_call(s, 4, "SetDtmfActions", {"leg_id": A, "actions": {"bindings": [
        {"dtmf_sequence": "*1", "action": "DTMF_ACTION_KIND_MUTE_TOGGLE"}, {"dtmf_sequence": "*2", "action": "DTMF_ACTION_KIND_MUTE"},
        {"dtmf_sequence": "*4", "action": "DTMF_ACTION_KIND_RECORDING_PAUSE"}], "locks": ["FACET_MUTE"]}}, ok={}, acked=4)

    def action(seq, leg_id, role, act, result, state):
        s.deliver("r1", seq, START, {"dtmf_action": {"leg_id": leg_id, "role": "LEG_ROLE_" + role,
                                                     "action": "DTMF_ACTION_KIND_" + act,
                                                     "result": "DTMF_ACTION_RESULT_" + result, "state": state}}, E1)

    s.inject("dtmf_action_match", leg_id=A, action="DTMF_ACTION_KIND_MUTE_TOGGLE", result="DTMF_ACTION_RESULT_LOCKED").note(
        "the mute is pinned: locked, unchanged")
    action(5, A, "AGENT", "MUTE_TOGGLE", "LOCKED", {"muted": True})
    s.inject("dtmf_action_match", leg_id=A, action="DTMF_ACTION_KIND_MUTE", result="DTMF_ACTION_RESULT_NOOP").note("already muted")
    action(6, A, "AGENT", "MUTE", "NOOP", {"muted": True})
    s.inject("dtmf_action_match", leg_id=A, action="DTMF_ACTION_KIND_RECORDING_PAUSE", result="DTMF_ACTION_RESULT_NOOP").note(
        "nothing records in this session: noop, and the state has no recording")
    action(7, A, "AGENT", "RECORDING_PAUSE", "NOOP", {"muted": True})
    sup = add_leg(owner(E1, acked=7), s.next_cmd(), leg_id=S, role="SUPERVISOR")
    sup["dtmf_actions"] = {"bindings": [{"dtmf_sequence": "*8", "action": "DTMF_ACTION_KIND_WHISPER"},
                                        {"dtmf_sequence": "*9", "action": "DTMF_ACTION_KIND_BARGE"}]}
    s.call("r1", "AddLeg", sup, ok={"state": "LEG_STATE_ORIGINATING"})
    s.inject("leg_answer", leg_id=S).deliver("r1", 8, START, leg(S, "SUPERVISOR", "ANSWERED", answered=START), E1)
    s.inject("dtmf_action_match", leg_id=S, action="DTMF_ACTION_KIND_WHISPER", result="DTMF_ACTION_RESULT_REFUSED").note(
        "a supervisor's mode needs a conference (IE step 14)")
    action(9, S, "SUPERVISOR", "WHISPER", "REFUSED", {})
    out.append(s)

    REC, REC2 = "0192f6a1-c000-7000-8000-000000000001", "0192f6a1-c000-7000-8000-000000000002"
    DEK = "ERERERERERERERERERERERERERERERERERERERERERE="  # 32 bytes of 0x11: a test value, no secret
    NULL_KEY = {"dek": DEK, "kek_id": "local-null-sink"}
    s = Script("recording-null-sink", "A recording on the null sink (local test setups only, until R9: nothing is written): "
               "StartRecording repeat-safe by recording_id and refused for a key meant for a real sink; an agent's DTMF pause "
               "and resume of the session's recording; EndSession finishes it, and its RecordingStopped (no path, size or "
               "hash; the pause listed) comes before its leg's end", ["coverage", "PCI"])
    answered_call(s)
    agent_codes = add_leg(owner(E1, acked=2), s.next_cmd())
    agent_codes["dtmf_actions"] = {"bindings": [
        {"dtmf_sequence": "*1", "action": "DTMF_ACTION_KIND_RECORDING_PAUSE"},
        {"dtmf_sequence": "*2", "action": "DTMF_ACTION_KIND_RECORDING_RESUME"},
        {"dtmf_sequence": "*3", "action": "DTMF_ACTION_KIND_MUTE_TOGGLE"}], "inter_digit_timeout_ms": 2000}
    s.call("r1", "AddLeg", agent_codes, ok={"state": "LEG_STATE_ORIGINATING"}).note(
        "the codes are on the agent's media before it rings")
    s.inject("leg_answer", leg_id=A).deliver("r1", 3, START, leg(A, "AGENT", "ANSWERED", answered=START), E1)
    media_call(s, 3, "StartRecording", {"leg_id": C, "recording_id": REC, "key": NULL_KEY}, ok={}, acked=3)
    s.deliver("r1", 4, START, {"recording_started": {"recording_id": REC, "leg_id": C}}, E1)
    media_call(s, 4, "StartRecording", {"leg_id": C, "recording_id": REC, "key": NULL_KEY}, ok={"existing": True}, acked=4)
    media_call(s, 4, "StartRecording", {"leg_id": A, "recording_id": REC2, "key": {"dek": DEK, "kek_id": "kek-2026-10"}},
               err=("FAILED_PRECONDITION", "invalid_state"), acked=4).note("a key meant for a real sink: never into the null one")
    s.call("r1", "Acknowledge", {"acks": [owner(E1, acked=4)]}, ok={}, tenant=None)
    s.advance(1000)
    s.inject("dtmf_action_match", leg_id=A, action="DTMF_ACTION_KIND_RECORDING_PAUSE", result="DTMF_ACTION_RESULT_APPLIED").note(
        "the agent keyed *1: the session's recording pauses (the customer's leg is the one recorded)")
    s.deliver("r1", 5, START + 1000, {"dtmf_action": {"leg_id": A, "role": "LEG_ROLE_AGENT",
                                                      "action": "DTMF_ACTION_KIND_RECORDING_PAUSE",
                                                      "result": "DTMF_ACTION_RESULT_APPLIED",
                                                      "state": {"recording": "RECORDING_STATE_PAUSED"}}}, E1)
    s.call("r1", "Acknowledge", {"acks": [owner(E1, acked=5)]}, ok={}, tenant=None)
    s.advance(2000)
    s.inject("dtmf_action_match", leg_id=A, action="DTMF_ACTION_KIND_RECORDING_RESUME", result="DTMF_ACTION_RESULT_APPLIED")
    s.deliver("r1", 6, START + 3000, {"dtmf_action": {"leg_id": A, "role": "LEG_ROLE_AGENT",
                                                      "action": "DTMF_ACTION_KIND_RECORDING_RESUME",
                                                      "result": "DTMF_ACTION_RESULT_APPLIED",
                                                      "state": {"recording": "RECORDING_STATE_ACTIVE"}}}, E1)
    s.call("r1", "Acknowledge", {"acks": [owner(E1, acked=6)]}, ok={}, tenant=None)
    s.advance(1000)
    media_call(s, 6, "EndSession", {"cause": "END_CAUSE_NORMAL"}, ok={}, acked=6)
    s.deliver("r1", 7, START + 4000, leg(A, "AGENT", "ENDED", cause="NORMAL", by="COMMAND", answered=START), E1)
    s.deliver("r1", 8, START + 4000, {"recording_stopped": {
        "recording_id": REC, "leg_id": C, "duration_ms": 4000, "kek_id": "local-null-sink",
        "pauses": [{"start_ms": s64(START + 1000), "end_ms": s64(START + 3000), "role": "LEG_ROLE_AGENT", "by_dtmf": True}]}},
              E1)
    s.deliver("r1", 9, START + 4000, leg(C, "CUSTOMER", "ENDED", cause="NORMAL", by="COMMAND", answered=START), E1)
    s.deliver("r1", 10, START + 4000, ended("ENDED_BY_OWNER"), E1)
    media_call(s, 10, "StopRecording", {"recording_id": REC}, ok={"already_gone": True}, acked=10)
    out.append(s)

    def invalid(name, description, violates, step_fn):
        s = Script(name, description, ["writer rules"], valid=False, violates=violates)
        s.watch("r1")
        step_fn(s)
        return s

    def answer_with(body, tenant=T, err=("INVALID_ARGUMENT", "invalid_request")):
        return lambda s: s.call("r1", "AnswerLeg", body, err=err, tenant=tenant)

    out.append(invalid("tenant-metadata-mismatch", "The tenant-id metadata names another tenant than the body", ["tenant_mismatch"],
                       answer_with({"owner": owner(E1), "command_id": "ie-bin-a-1:0001", "leg_id": C}, tenant=T2,
                                   err=("INVALID_ARGUMENT", "tenant_mismatch"))))
    out.append(invalid("no-owner", "A command without its owner", ["missing_owner", "tenant_mismatch"],
                       answer_with({"command_id": "ie-bin-a-1:0001", "leg_id": C}, err=("INVALID_ARGUMENT", "tenant_mismatch"))))
    out.append(invalid("epoch-zero", "A command with owner_epoch 0", ["bad_epoch"],
                       answer_with({"owner": owner(0) | {"owner_epoch": "0"}, "command_id": "ie-bin-a-1:0001", "leg_id": C})))
    out.append(invalid("bad-command-id", "A command_id with a space", ["bad_command_id"],
                       answer_with({"owner": owner(E1), "command_id": "ie-bin-a-1 0001", "leg_id": C})))
    out.append(invalid("leg-id-not-uuid", "A leg_id that isn't a UUID", ["bad_uuid"],
                       answer_with({"owner": owner(E1), "command_id": "ie-bin-a-1:0001", "leg_id": "leg-1"})))
    out.append(invalid("reject-with-200", "RejectLeg with a success status", ["bad_sip_status"],
                       lambda s: s.call("r1", "RejectLeg", {"owner": owner(E1), "command_id": "ie-bin-a-1:0001", "leg_id": C,
                                                            "sip_status": 200}, err=("INVALID_ARGUMENT", "invalid_request"))))
    return out


DEFERRED = {
    "description": "Names of telephony.v1 that have no script yet, each with the step that adds its scripts and implementation (TC ADR 0001 §6). contract-check exempts them from coverage and fails if one gets a valid script, so this list only shrinks.",
    "deferred": [
        {"kind": "rpc", "name": "SetIsolation", "step": "consults (IE step 11)"},
        {"kind": "rpc", "name": "SetMonitorMode", "step": "supervisor modes (IE step 14)"},
    ],
}


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out = root / "fixtures" / "telephony.v1"
    if out.exists():
        shutil.rmtree(out)
    (out / "valid").mkdir(parents=True)
    (out / "invalid").mkdir(parents=True)
    all_scripts = scripts()
    for s in all_scripts:
        d = s.doc_out()
        where = "valid" if d["writer_valid"] else "invalid"
        (out / where / f"{s.name}.json").write_text(json.dumps(d, indent=2) + "\n")
    (out / "deferred.json").write_text(json.dumps(DEFERRED, indent=2) + "\n")
    print(f"{len(all_scripts)} scripts in {out}")


if __name__ == "__main__":
    main()
