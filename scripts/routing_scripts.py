#!/usr/bin/env python3
"""Writes the executable routing.v1 scripts (G5) into fixtures/routing.v1/.

The scripts are data; this file is how they were written, so their owners (interaction-engine and
parity-cc-routing) can change one and regenerate them. It writes only the files named here. Since
0.9.0 it also writes 14 of the 0.1.0 reader notes with a `given` (parity-cc-routing M2): each keeps
its original steps, ids, times and notes, and gains the setup steps that bring RE to the note's
starting point (a Watch, the Enqueue, the agent's answer, the clock). Two corrections, both rules
the notes predate: a RESERVED or OVERFLOWED entry shows no position (G5), and a replay to the
adopter carries the current owner fence (CR-054). 0.11.0 (phase34): the other 7 notes are
executable too (callbacks, voicemail tasks, direct and consult targets, a supervisor's forced
state); race-run-callback-resent's redelivery follows the 5 s RE waits for an ack.
Run from the repository root: python3 -I scripts/routing_scripts.py

A script with `given` is executable: a harness seeds RE with `given` (a virtual clock at
`start_ms`, the bin's tenants, queues and agents), then plays the steps in order: `call` (compare
the answer with `expect`), `deliver` (the next message on that stream), `advance` (the clock) and
`inject` (what an agent does, through agents.v1). CONVENTIONS.md "Executable routing scripts".
"""
import json
import pathlib
import sys

PKG = "parity.routing.v1"
SVC = PKG + ".RoutingService"
T, T2 = "acme", "globex"
I = "0192f6a1-7c3e-7b21-9a4d-5e6f70819203"
I2 = "0192f6a1-7c3e-7b21-9a4d-5e6f70819204"
E = "0192f6a1-9000-7aaa-8bbb-000000000001"   # placeholders for ids RE makes: a runner binds them
E2 = "0192f6a1-9000-7aaa-8bbb-000000000002"   # at first sight
R = "0192f6a1-9100-7ccc-8ddd-000000000001"
R2 = "0192f6a1-9100-7ccc-8ddd-000000000002"
START = 1792800000000
LEADER = 7
EP1, EP2, EP3 = 1042, 1052, 1062            # etcd create revisions: unique across interactions
R1 = "ie-bin-a-1"
R2_REPLICA = "ie-bin-a-2"                    # the adopter (IE steps 7-8)
REPLICAS = {"r1": R1, "r2": R2_REPLICA}
I3 = "0192f6a1-7c3e-7b21-9a4d-5e6f70819205"
E3 = "0192f6a1-9000-7aaa-8bbb-000000000003"
G = "0192f6a3-5d6e-7f80-9112-233445566778"   # a globex interaction
EG = "0192f6a3-9000-7aaa-8bbb-0000000000ff"  # its entry and reservation (placeholders)
RG = "0192f6a3-9100-7ccc-8ddd-0000000000ff"
EPA, EPG, EPS = 1043, 1072, 1041            # the adopter's raise, globex's owner, a stale owner
A7 = "agent-7f3a"
CK = "ck_3q2-xV9mZr8LbN0pQ7sT1w"            # a customer key (opaque, no personal data)
BIN = "bin-a"
TP = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
OFFER_TIMEOUT = 15000
DEADLINE = 30000


def s64(n):
    return str(n)


def contact(tenant, agent):
    return f"sip:{agent}@{tenant}.agents.example"


def agent(tenant, agent_id, ready_since=None, queues=("sales",)):
    a = {"tenant_id": tenant, "agent_id": agent_id, "agent_contact": contact(tenant, agent_id),
         "queues": list(queues)}
    if ready_since is not None:
        a["ready_since_ms"] = s64(ready_since)
    return a


class Script:
    def __init__(self, name, description, pins, agents, tenants=(T,), queues=None, valid=True,
                 violates=None, streams=("r1",)):
        self.name = name
        self.valid = valid
        self.doc = {"description": description, "kind": "grpc", "writer_valid": valid, "package": PKG}
        if violates:
            self.doc["violates"] = violates
        self.doc["pins"] = pins
        self.doc["streams"] = {lbl: {"replica_id": REPLICAS[lbl], "bin": BIN} for lbl in streams}
        self.doc["given"] = {
            "start_ms": s64(START), "leader_epoch": s64(LEADER), "bin": BIN, "tenants": list(tenants),
            "queues": queues or [{"tenant_id": t, "queue_id": "sales"} for t in tenants],
            "agents": agents, "offer_timeout_ms": s64(OFFER_TIMEOUT),
            "assignment_deadline_ms": s64(DEADLINE),
        }
        self.steps = []
        self.now = START

    def note(self, text):
        self.steps[-1]["note"] = text
        return self

    def watch(self, label="r1"):
        st = self.doc["streams"][label]
        self.steps.append({
            "op": "call", "stream": label, "method": SVC + "/Watch", "message": PKG + ".WatchRequest",
            "metadata": {"traceparent": TP}, "body": dict(st),
            "expect": {"message": PKG + ".WatchResponse",
                       "body": {"leader_epoch": s64(LEADER), "delivery_seq": "0",
                                "heartbeat": {"at_ms": s64(self.now)}}}})
        return self

    def advance(self, ms):
        self.steps.append({"op": "advance", "ms": s64(ms)})
        self.now += ms
        return self

    def answers(self, tenant, agent_id, decision, result):
        self.steps.append({"op": "inject", "what": "agent_answers",
                           "args": {"tenant_id": tenant, "agent_id": agent_id,
                                    "decision": decision, "result": result}})
        return self

    def call(self, rpc, body, ok=None, err=None, tenant=T):
        step = {"op": "call", "method": f"{SVC}/{rpc}", "message": f"{PKG}.{rpc}Request",
                "metadata": {"tenant-id": tenant, "traceparent": TP}, "body": body}
        if err:
            step["expect"] = {"status": err[0], "parity_error": err[1]}
        else:
            step["expect"] = {"message": f"{PKG}.{rpc}Response", "body": ok if ok is not None else {}}
        self.steps.append(step)
        return self

    def inject(self, what, args):
        self.steps.append({"op": "inject", "what": what, "args": args})
        return self

    def deliver(self, seq, event, label="r1"):
        body = {"leader_epoch": s64(LEADER), "delivery_seq": s64(seq)}
        body.update(event)
        self.steps.append({"op": "deliver", "stream": label, "message": PKG + ".WatchResponse",
                           "body": body})
        return self

    def write(self, root):
        self.doc["steps"] = self.steps
        sub = "valid" if self.valid else "invalid"
        path = root / "fixtures" / "routing.v1" / sub / f"{self.name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.doc, indent=2, ensure_ascii=False) + "\n")
        return path


def owner(acked=0, epoch=EP1, interaction=I, tenant=T, replica=R1):
    return {"tenant_id": tenant, "interaction_id": interaction, "owner_epoch": s64(epoch),
            "replica_id": replica, "acked_delivery_seq": s64(acked)}


def enqueue(acked=0, epoch=EP1, interaction=I, queue="sales", at=START, tenant=T):
    return {"owner": owner(acked, epoch, interaction, tenant), "enqueue_seq": 1,
            "target": {"queue_id": queue}, "priority": 0,
            "first_queued_at_ms": s64(at), "ordering_at_ms": s64(at), "channel": "CHANNEL_VOICE",
            "context": {"language": "en-GB"}, "reason": "ENQUEUE_REASON_NEW",
            "placement": "PLACEMENT_BY_ORDERING_TIME"}


def entry(entry_id=E, queue="sales", at=START, state="ENTRY_STATE_WAITING", position=1):
    e = {"entry_id": entry_id, "enqueue_seq": 1, "target": {"queue_id": queue},
         "first_queued_at_ms": s64(at), "ordering_at_ms": s64(at), "state": state}
    if position:
        e["position"] = position
    return e


def assigned(agent_id, accepted_at, reservation=R, entry_id=E, epoch=EP1, interaction=I, tenant=T):
    return {"assigned": {
        "target": {"tenant_id": tenant, "interaction_id": interaction, "owner_epoch": s64(epoch)},
        "reservation_id": reservation, "entry_id": entry_id, "agent_id": agent_id,
        "agent_contact": contact(tenant, agent_id), "deadline_ms": s64(accepted_at + DEADLINE)}}


def note_enqueue(seq=1, queue="sales", at=START + 5000, ordering=None, reason="ENQUEUE_REASON_NEW",
                 acked=0, epoch=EP1, replica=R1, interaction=I, tenant=T, reqs=True):
    """An Enqueue as the 0.1.0 notes write it (requirements and a customer key in the context)."""
    b = {"owner": owner(acked, epoch, interaction, tenant, replica), "enqueue_seq": seq,
         "target": {"queue_id": queue}}
    if reqs:
        b["requirements"] = [{"skill_id": "sales-en", "min_level": 2}]
    b.update({"priority": 0, "first_queued_at_ms": s64(at), "ordering_at_ms": s64(ordering or at),
              "channel": "CHANNEL_VOICE", "context": {"language": "en-GB", "customer_key": CK},
              "reason": reason, "placement": "PLACEMENT_BY_ORDERING_TIME"})
    return b


def note_entry(entry_id=E, queue="sales", at=START + 5000, ordering=None, state="ENTRY_STATE_WAITING",
               position=1, seq=1):
    e = {"entry_id": entry_id, "enqueue_seq": seq, "target": {"queue_id": queue},
         "first_queued_at_ms": s64(at), "ordering_at_ms": s64(ordering or at), "state": state}
    if state == "ENTRY_STATE_WAITING":  # G5: no position (or estimate) once RESERVED or OVERFLOWED
        e["position"] = position
        e["expected_wait_s"] = 40
    return e


def note_assigned(accepted_at=START + 30000, agent_id=A7, reservation=R, entry_id=E, epoch=EP1,
                  interaction=I, tenant=T):
    a = assigned(agent_id, accepted_at, reservation, entry_id, epoch, interaction, tenant)
    a["assigned"]["consult"] = False
    return a


def overflow(action, queue_id=None, epoch=EP1):
    o = {"target": {"tenant_id": T, "interaction_id": I, "owner_epoch": s64(epoch)}, "entry_id": E,
         "action": action}
    if queue_id:
        o["queue_id"] = queue_id
    return {"overflow": o}


def ready_and_accept(s, agent_id=A7):
    """agent-7f3a logs in ready at +30 s (an offer made earlier would expire before the notes'
    accept) and accepts: Assigned.deadline_ms = +60 s, as in the notes."""
    s.inject("agent_ready", {"tenant_id": T, "agent_id": agent_id})
    s.answers(T, agent_id, "DECISION_ACCEPT", "accepted")


def notes():
    """The 0.1.0 reader notes RE can play in phase 1 (written executable in 0.9.0)."""
    out = []
    q = lambda qid, **kw: {"tenant_id": T, "queue_id": qid, **kw}  # noqa: E731

    s = Script("enqueue-new", "A caller's first queue", ["coverage"], [])
    s.watch().advance(3000)
    s.call("Enqueue", enqueue(epoch=EP2, interaction=I2, at=START + 3000),
           {"entry": entry(entry_id=E2, at=START + 3000), "existing": False}).note("a caller ahead")
    s.advance(1000)
    s.call("Enqueue", enqueue(epoch=EP3, interaction=I3, at=START + 4000),
           {"entry": entry(entry_id=E3, at=START + 4000, position=2), "existing": False}).note(
        "another caller ahead")
    s.advance(1000)
    s.call("Enqueue", note_enqueue(), {"entry": note_entry(position=3), "existing": False})
    out.append(s)

    s = Script("enqueue-transfer-new-ordering",
               "An agent's blind transfer: a new ordering time, the same first_queued_at_ms",
               ["queue-ordering ruling"], [], queues=[q("sales"), q("support")])
    s.watch().advance(5000)
    s.call("Enqueue", note_enqueue(), {"entry": note_entry(), "existing": False}).note(
        "the caller's first queue")
    s.advance(135000)
    s.call("Enqueue", note_enqueue(seq=2, queue="support", ordering=START + 140000,
                                   reason="ENQUEUE_REASON_TRANSFER"),
           {"entry": note_entry(entry_id=E2, queue="support", ordering=START + 140000, seq=2),
            "existing": False})
    out.append(s)

    s = Script("position-and-heartbeat",
               "Position updates and heartbeats carry delivery_seq 0 and are never replayed",
               ["coverage", "F2"], [])
    s.watch().advance(3000)
    s.call("Enqueue", enqueue(epoch=EP2, interaction=I2, at=START + 3000),
           {"entry": entry(entry_id=E2, at=START + 3000), "existing": False})
    s.advance(1000)
    s.call("Enqueue", enqueue(epoch=EP3, interaction=I3, at=START + 4000),
           {"entry": entry(entry_id=E3, at=START + 4000, position=2), "existing": False})
    s.advance(1000)
    s.call("Enqueue", note_enqueue(), {"entry": note_entry(position=3), "existing": False})
    s.call("Dequeue", {"owner": owner(0, EP3, I3), "entry_id": E3, "reason": "DEQUEUE_REASON_ABANDONED"},
           {"already_gone": False}).note("the caller ahead of this one hangs up")
    s.deliver(0, {"position": {"target": {"tenant_id": T, "interaction_id": I, "owner_epoch": s64(EP1)},
                               "entry_id": E, "position": 2, "expected_wait_s": 25}})
    s.advance(56000)
    s.deliver(0, {"heartbeat": {"at_ms": s64(START + 61000)}})
    out.append(s)

    s = Script("race-adoption-after-move",
               "RE moved the entry by an overflow; the adopter's Reattach sees the move and its "
               "repeated Enqueue gets the moved entry", ["F9"], [],
               queues=[q("sales", max_wait_ms=s64(30000), max_wait_action="OVERFLOW_ACTION_QUEUE",
                         max_wait_queue_id="sales-overflow"), q("sales-overflow")],
               streams=("r1", "r2"))
    s.watch().advance(4000)
    s.call("Enqueue", enqueue(epoch=EP2, interaction=I2, queue="sales-overflow", at=START + 4000),
           {"entry": entry(entry_id=E2, queue="sales-overflow", at=START + 4000), "existing": False}).note(
        "a caller already in sales-overflow")
    s.advance(1000)
    s.call("Enqueue", note_enqueue(), {"entry": note_entry(), "existing": False})
    s.advance(30000).note("sales' max wait (30 s): the entry moves to sales-overflow (L14)")
    s.deliver(1, overflow("OVERFLOW_ACTION_QUEUE", "sales-overflow"))
    s.inject("stream_drop", {"stream": "r1"}).note("ie-bin-a-1 dies")
    s.watch("r2")
    s.call("Reattach", {"owner": owner(0, EPA, replica=R2_REPLICA)},
           {"entry": note_entry(queue="sales-overflow", position=2), "reservations": [],
            "last_delivery_seq": "1"})
    s.deliver(1, overflow("OVERFLOW_ACTION_QUEUE", "sales-overflow", epoch=EPA), "r2").note(
        "replayed to the adopter with the current fence (CR-054, 0.9.0)")
    s.call("Enqueue", note_enqueue(acked=1, epoch=EPA, replica=R2_REPLICA),
           {"entry": note_entry(queue="sales-overflow", position=2), "existing": True}).note(
        "the same enqueue_seq with the original target: not enqueue_conflict")
    out.append(s)

    s = Script("race-caller-gone-while-assigned", "The caller hangs up while the agent's leg rings",
               ["F1 L5"], [agent(T, A7)])
    s.watch().advance(5000)
    s.call("Enqueue", enqueue(at=START + 5000), {"entry": entry(at=START + 5000), "existing": False})
    s.advance(25000)
    ready_and_accept(s)
    s.deliver(1, note_assigned())
    s.call("ReportOutcome", {"owner": owner(1), "reservation_id": R, "outcome": "OUTCOME_CALLER_GONE"},
           {"existing": False, "recorded": "OUTCOME_CALLER_GONE"})
    s.call("Dequeue", {"owner": owner(1), "entry_id": E, "reason": "DEQUEUE_REASON_ABANDONED"},
           {"already_gone": True})
    out.append(s)

    s = Script("race-expiry-paused-during-adoption",
               "IE dies during OFFERING: RE pauses the expiry (at most 10 s) until the adopter's "
               "stream serves the interaction", ["N1"], [agent(T, A7)], streams=("r1", "r2"))
    s.watch().advance(5000)
    s.call("Enqueue", enqueue(at=START + 5000), {"entry": entry(at=START + 5000), "existing": False})
    s.advance(25000)
    ready_and_accept(s)
    s.deliver(1, note_assigned())
    s.inject("stream_drop", {"stream": "r1"}).note("ie-bin-a-1 dies before deadline_ms")
    s.advance(38000).note("deadline_ms + 2 s (+62 s) passes with no stream: the expiry is paused")
    s.watch("r2")
    s.call("Reattach", {"owner": owner(0, EPA, replica=R2_REPLICA)},
           {"entry": note_entry(state="ENTRY_STATE_RESERVED"),
            "reservations": [{"reservation_id": R, "agent_id": A7, "agent_contact": contact(T, A7),
                              "deadline_ms": s64(START + 60000), "state": "RESERVATION_STATE_ASSIGNED",
                              "consult": False}],
            "last_delivery_seq": "1"}).note(
        "the old replica's stream closed before deadline_ms; deadline_ms + 2 s passed during the "
        "adoption; expiry was paused, 6 s of the 10 s budget used")
    s.deliver(1, note_assigned(epoch=EPA), "r2").note("replayed with the current fence (CR-054, 0.9.0)")
    s.call("ReportOutcome", {"owner": owner(1, EPA, replica=R2_REPLICA), "reservation_id": R,
                             "outcome": "OUTCOME_CONNECTED"},
           {"existing": False, "recorded": "OUTCOME_CONNECTED"}).note("the adopter found the agent's leg answered")
    out.append(s)

    s = Script("race-foreign-ids",
               "Another interaction's reservation, another tenant's entry: refused, nothing touched",
               ["F1 L12"], [agent(T2, "agent-g1", START - 20000)], tenants=(T, T2))
    s.watch()
    s.call("Enqueue", enqueue(epoch=EPG, interaction=G, tenant=T2),
           {"entry": entry(entry_id=EG), "existing": False}, tenant=T2).note("a globex caller")
    s.answers(T2, "agent-g1", "DECISION_ACCEPT", "accepted")
    s.deliver(1, assigned("agent-g1", START, RG, EG, EPG, G, T2))
    s.call("Enqueue", enqueue(), {"entry": entry(), "existing": False}).note("acme's own caller")
    s.call("ReportOutcome", {"owner": owner(0), "reservation_id": RG, "outcome": "OUTCOME_CONNECTED"},
           err=("NOT_FOUND", "reservation_unknown"))
    s.call("Dequeue", {"owner": owner(0), "entry_id": EG, "reason": "DEQUEUE_REASON_ABANDONED"},
           err=("NOT_FOUND", "entry_unknown")).note(
        "the entry belongs to globex interaction 0192f6a3-5d6e-7f80-9112-233445566778")
    s.call("ReportOutcome", {"owner": owner(1, EPG, G, T2), "reservation_id": RG,
                             "outcome": "OUTCOME_CONNECTED"},
           {"existing": False, "recorded": "OUTCOME_CONNECTED"}, tenant=T2).note(
        "nothing was touched: globex's reservation is still ASSIGNED")
    out.append(s)

    s = Script("race-late-connected-after-expiry",
               "CONNECTED arrives after RE expired the offer: RE answers EXPIRED and IE drops the agent's leg",
               ["F1 L7", "F1 L9"], [agent(T, A7)])
    s.watch().advance(5000)
    s.call("Enqueue", enqueue(at=START + 5000), {"entry": entry(at=START + 5000), "existing": False})
    s.advance(25000)
    ready_and_accept(s)
    s.deliver(1, note_assigned())
    s.advance(32000)
    s.deliver(2, {"revoke": {"target": {"tenant_id": T, "interaction_id": I, "owner_epoch": s64(EP1)},
                             "reservation_id": R, "reason": "expired"}}).note(
        "deadline_ms + 2 s passed with a live stream and no outcome")
    s.call("ReportOutcome", {"owner": owner(1), "reservation_id": R, "outcome": "OUTCOME_CONNECTED"},
           {"existing": False, "recorded": "OUTCOME_EXPIRED"}).note("IE hangs up the agent's leg and stays QUEUED")
    out.append(s)

    s = Script("race-missed-offer-keeps-ordering",
               "A missed offer puts the entry back at its original ordering time",
               ["F1 L3", "queue-ordering ruling"], [agent(T, A7)])
    s.watch().advance(5000)
    s.call("Enqueue", enqueue(at=START + 5000), {"entry": entry(at=START + 5000), "existing": False})
    s.advance(25000)
    ready_and_accept(s)
    s.deliver(1, note_assigned())
    s.call("ReportOutcome", {"owner": owner(1), "reservation_id": R, "outcome": "OUTCOME_AGENT_NO_ANSWER",
                             "detail": "timeout"},
           {"existing": False, "recorded": "OUTCOME_AGENT_NO_ANSWER"})
    s.call("Reattach", {"owner": owner(1, EPA, replica=R2_REPLICA)},
           {"entry": note_entry(), "reservations": [], "last_delivery_seq": "1"}).note(
        "read back by an adopter: still WAITING at the original ordering_at_ms")
    out.append(s)

    for name, desc, pins, retry in (
            ("race-overflow-kept-until-dequeue",
             "An overflowed entry is kept, offered to no one, until Dequeue(OVERFLOW)", ["F2", "F1 L13"], False),
            ("race-overflow-replay-after-checkpoint",
             "IE checkpointed the voicemail overflow, then died before its Dequeue: the adopter "
             "resumes the voicemail, never restarts it", ["N4"], True)):
        s = Script(name, desc, pins, [],
                   queues=[q("sales", max_wait_ms=s64(30000), max_wait_action="OVERFLOW_ACTION_VOICEMAIL")],
                   streams=("r1", "r2"))
        s.watch().advance(5000)
        s.call("Enqueue", note_enqueue(), {"entry": note_entry(), "existing": False})
        s.advance(30000).note("sales' max wait (30 s): voicemail (L13)")
        s.deliver(1, overflow("OVERFLOW_ACTION_VOICEMAIL"))
        if retry:
            s.note("IE checkpoints entry_id + action, then dies before Dequeue(OVERFLOW)")
        s.inject("stream_drop", {"stream": "r1"}).note("ie-bin-a-1 dies")
        s.watch("r2")
        s.call("Reattach", {"owner": owner(0, EPA, replica=R2_REPLICA)},
               {"entry": note_entry(state="ENTRY_STATE_OVERFLOWED"), "reservations": [],
                "last_delivery_seq": "1"})
        if not retry:
            s.note("IE died before checkpointing the overflow; the Overflow is replayed to the adopter")
        # The notes' own text; the replay carries the current fence (CR-054, 0.9.0).
        s.deliver(1, overflow("OVERFLOW_ACTION_VOICEMAIL", epoch=EPA), "r2").note(
            "replayed; the adopter finds the same entry_id and action in its checkpoint: the "
            "recording carries on" if retry else "replayed to ie-bin-a-2")
        s.call("Dequeue", {"owner": owner(1, EPA, replica=R2_REPLICA), "entry_id": E,
                           "reason": "DEQUEUE_REASON_OVERFLOW"}, {"already_gone": False})
        if retry:
            s.call("Dequeue", {"owner": owner(1, EPA, replica=R2_REPLICA), "entry_id": E,
                               "reason": "DEQUEUE_REASON_OVERFLOW"}, {"already_gone": True}).note(
                "a retry after a lost answer")
        out.append(s)

    s = Script("race-replay-after-reconnect",
               "Unacknowledged messages are replayed when the stream reopens; IE drops the duplicate by delivery_seq",
               ["F2"], [agent(T, A7)])
    s.advance(5000)
    s.call("Enqueue", enqueue(at=START + 5000), {"entry": entry(at=START + 5000), "existing": False}).note(
        "IE's stream isn't up yet")
    s.advance(25000)
    ready_and_accept(s)
    s.advance(31000)
    s.watch()
    s.deliver(1, note_assigned())
    s.inject("stream_drop", {"stream": "r1"})
    s.watch().note("the stream dropped; IE reconnects")
    s.deliver(1, note_assigned()).note("replayed on the new stream; IE has already processed delivery_seq 1 and drops it")
    s.call("ReportOutcome", {"owner": owner(1), "reservation_id": R, "outcome": "OUTCOME_CONNECTED"},
           {"existing": False, "recorded": "OUTCOME_CONNECTED"})
    out.append(s)

    s = Script("stale-owner", "A fenced-off owner's late report is refused", ["fence"],
               [agent(T, A7, START - 20000)])
    s.watch()
    s.call("Enqueue", enqueue(), {"entry": entry(), "existing": False})
    s.answers(T, A7, "DECISION_ACCEPT", "accepted")
    s.deliver(1, assigned(A7, START))
    s.call("ReportOutcome", {"owner": owner(0, EPS, replica="ie-bin-a-3"), "reservation_id": R,
                             "outcome": "OUTCOME_CONNECTED"}, err=("ABORTED", "stale_owner"))
    out.append(s)

    s = Script("wrap-up-ended", "The agent's wrap-up ends: IE can emit closed{disposition}", ["F7", "F1 L11"],
               [agent(T, A7, START - 20000, ("support",))],
               queues=[q("sales", max_wait_ms=s64(30000), max_wait_action="OVERFLOW_ACTION_QUEUE",
                         max_wait_queue_id="support"), q("support")])
    s.watch().advance(5000)
    s.call("Enqueue", enqueue(at=START + 5000), {"entry": entry(at=START + 5000), "existing": False})
    s.advance(30000).note("sales' max wait: the entry moves to support, where agent-7f3a works")
    s.deliver(1, overflow("OVERFLOW_ACTION_QUEUE", "support"))
    s.answers(T, A7, "DECISION_ACCEPT", "accepted")
    s.deliver(2, note_assigned(accepted_at=START + 35000))
    s.call("ReportOutcome", {"owner": owner(2), "reservation_id": R, "outcome": "OUTCOME_CONNECTED"},
           {"existing": False, "recorded": "OUTCOME_CONNECTED"})
    s.advance(385000)
    s.call("CallEnded", {"owner": owner(2), "agent_id": A7, "reservation_id": R,
                         "ended_at_ms": s64(START + 420000)}, {"existing": False})
    s.advance(60000).note("the queue's wrap-up limit (60 s)")
    s.deliver(3, {"wrap_up_ended": {"target": {"tenant_id": T, "interaction_id": I, "owner_epoch": s64(EP1)},
                                    "reservation_id": R, "agent_id": A7, "ended_at_ms": s64(START + 480000)}})
    s.steps.append({"op": "call", "method": f"{SVC}/Acknowledge", "message": f"{PKG}.AcknowledgeRequest",
                    "metadata": {"traceparent": TP},
                    "body": {"acks": [{"tenant_id": T, "interaction_id": I, "owner_epoch": s64(EP1),
                                       "replica_id": R1, "acked_delivery_seq": "3"}]},
                    "expect": {"message": f"{PKG}.AcknowledgeResponse", "body": {"ignored": 0}}})
    out.append(s)
    return out


def scripts():
    out = []

    s = Script("race-offer-timeout-vs-accept",
               "An offer not answered by expires_at_ms goes to the next agent; a late accept loses; "
               "the entry keeps its enqueue time",
               ["G5", "agents offer expiry", "L1", "queue-ordering ruling"],
               [agent(T, "agent-a1", START - 20000), agent(T, "agent-a2", START - 10000)])
    s.watch()
    s.call("Enqueue", enqueue(), {"entry": entry(), "existing": False})
    s.answers(T, "agent-a2", "DECISION_ACCEPT", "no_offer").note(
        "the offer went to agent-a1, ready the longest")
    s.advance(OFFER_TIMEOUT).note("now = the offer's expires_at_ms")
    s.answers(T, "agent-a1", "DECISION_ACCEPT", "offer_gone").note(
        "an answer at or after expires_at_ms loses: the offer expired (agent-a1: not ready, missed_offer)")
    s.answers(T, "agent-a2", "DECISION_ACCEPT", "accepted").note("re-offered to the next agent")
    s.deliver(1, assigned("agent-a2", s.now))
    s.call("Enqueue", enqueue(acked=1),
           {"entry": entry(state="ENTRY_STATE_RESERVED", position=0), "existing": True}).note(
        "a retried Enqueue reads the entry back: RESERVED, with its original first_queued_at_ms "
        "and ordering_at_ms")
    out.append(s)

    s = Script("race-abandon-vs-assign",
               "The caller hangs up while Assigned is on its way: Dequeue(ABANDONED) ends the "
               "reservation as CALLER_GONE and frees the agent; IE's later CALLER_GONE is a repeat",
               ["G5", "L5", "L16", "L17"],
               [agent(T, "agent-a1", START - 20000)])
    s.watch()
    s.call("Enqueue", enqueue(), {"entry": entry(), "existing": False})
    s.answers(T, "agent-a1", "DECISION_ACCEPT", "accepted")
    s.deliver(1, assigned("agent-a1", s.now))
    s.call("Dequeue", {"owner": owner(0), "entry_id": E, "reason": "DEQUEUE_REASON_ABANDONED"},
           {"already_gone": False}).note(
        "IE hasn't processed Assigned (acked_delivery_seq 0): RE removes the entry, records "
        "CALLER_GONE for the reservation and frees agent-a1")
    s.call("Enqueue", enqueue(epoch=EP2, interaction=I2),
           {"entry": entry(entry_id=E2), "existing": False}).note("another caller")
    s.answers(T, "agent-a1", "DECISION_ACCEPT", "accepted").note("agent-a1 was freed")
    s.deliver(1, assigned("agent-a1", s.now, reservation=R2, entry_id=E2, epoch=EP2, interaction=I2))
    s.call("ReportOutcome", {"owner": owner(1), "reservation_id": R, "outcome": "OUTCOME_CALLER_GONE",
                             "detail": "ended"},
           {"existing": True, "recorded": "OUTCOME_CALLER_GONE"}).note(
        "IE processes the first Assigned for a call that is gone: a repeat of the recorded outcome")
    out.append(s)

    s = Script("race-duplicate-enqueue",
               "A retried Enqueue (the first answer was lost) returns the same entry; one entry, "
               "one offer, one Assigned; the same enqueue_seq for another queue is a conflict",
               ["G5", "Enqueue repeat-safe by (interaction, enqueue_seq)"],
               [agent(T, "agent-a1", START - 20000, ("sales", "support")),
                agent(T, "agent-a2", START - 10000, ("sales", "support"))],
               queues=[{"tenant_id": T, "queue_id": "sales"}, {"tenant_id": T, "queue_id": "support"}])
    s.watch()
    s.call("Enqueue", enqueue(), {"entry": entry(), "existing": False})
    s.call("Enqueue", enqueue(), {"entry": entry(), "existing": True}).note(
        "the same request again: the entry made first")
    s.call("Enqueue", enqueue(queue="support"), err=("ALREADY_EXISTS", "enqueue_conflict")).note(
        "the same enqueue_seq with a target RE never moved the entry to")
    s.answers(T, "agent-a2", "DECISION_ACCEPT", "no_offer").note("no second entry was offered")
    s.answers(T, "agent-a1", "DECISION_ACCEPT", "accepted")
    s.deliver(1, assigned("agent-a1", s.now))
    s.call("Enqueue", enqueue(acked=1),
           {"entry": entry(state="ENTRY_STATE_RESERVED", position=0), "existing": True})
    out.append(s)

    s = Script("race-cross-tenant-agent",
               "An acme call is never offered to a globex agent, even one ready longer on a queue "
               "of the same name; globex can't see or fence acme's interaction",
               ["G5", "L12", "tenant isolation"],
               [agent(T2, "agent-g1", START - 60000), agent(T, "agent-a1", START - 1000)],
               tenants=(T, T2))
    s.watch()
    s.call("Enqueue", enqueue(), {"entry": entry(), "existing": False})
    s.answers(T2, "agent-g1", "DECISION_ACCEPT", "no_offer").note(
        "globex's agent, ready the longest, on globex's own 'sales'")
    s.answers(T, "agent-a1", "DECISION_ACCEPT", "accepted")
    s.deliver(1, assigned("agent-a1", s.now))
    s.call("ReportOutcome", {"owner": owner(0, epoch=EP3, tenant=T2), "reservation_id": R,
                             "outcome": "OUTCOME_CALLER_GONE"},
           err=("NOT_FOUND", "reservation_unknown"), tenant=T2).note(
        "globex names acme's interaction and reservation, with a higher owner_epoch")
    s.call("ReportOutcome", {"owner": owner(1), "reservation_id": R, "outcome": "OUTCOME_CONNECTED"},
           {"existing": False, "recorded": "OUTCOME_CONNECTED"}).note(
        "acme's owner isn't fenced by globex's epoch: entries, reservations and fences are per "
        "(tenant, interaction)")
    out.append(s)

    s = Script("tenant-mismatch",
               "The tenant-id metadata names another tenant than the owner's",
               ["G5", "routing writer rules"], [agent(T, "agent-a1", START - 20000)],
               valid=False, violates=["tenant_mismatch"])
    s.call("Enqueue", enqueue(), err=("INVALID_ARGUMENT", "tenant_mismatch"), tenant=T2)
    out.append(s)
    return out



CB = "0192f6b0-2000-7a00-8b00-000000000001"   # a callback id (RE makes it: a placeholder)
IXC = "0192f6b0-1c2d-7e3f-a405-162738495a6b"  # a callback's interaction (RE makes it: a placeholder)
RC = "0192f6b0-2100-7a00-8b00-000000000001"   # a callback's reservation (placeholder)
TASK = "0192f6c0-0000-7a00-8b00-000000000001"  # a voicemail task (placeholder)
E4 = "0192f6a1-9000-7aaa-8bbb-000000000004"
A9 = "agent-9b1c"
IX9 = "0192f6a2-0b4c-7d10-8e2f-3a4b5c6d7e8f"
NUMBER = "+447700900123"


def phase34():
    """0.11.0 (parity-cc-routing phases 3 and 4): the last 7 reader notes become executable (each keeps
    its steps, ids and notes; the setup steps before them reach its starting point), and two new
    scripts. RE-made ids: entry_id, reservation_id, callback_id, task_id and RunCallback's
    interaction_id are placeholders."""
    out = []
    q = lambda qid, **kw: {"tenant_id": T, "queue_id": qid, **kw}  # noqa: E731

    s = Script("convert-to-callback", "The caller takes the callback offered on overflow", ["coverage", "F1 L15"], [],
               queues=[q("sales", max_wait_ms="10000", max_wait_action="OVERFLOW_ACTION_QUEUE", max_wait_queue_id="sales-2"),
                       q("sales-2", max_wait_ms="10000", max_wait_action="OVERFLOW_ACTION_QUEUE", max_wait_queue_id="sales-3"),
                       q("sales-3", max_wait_ms="10000", max_wait_action="OVERFLOW_ACTION_QUEUE", max_wait_queue_id="sales-4"),
                       q("sales-4", max_wait_ms="10000", max_wait_action="OVERFLOW_ACTION_CALLBACK_OFFER")])
    s.watch().advance(5000)
    s.call("Enqueue", note_enqueue(), {"entry": note_entry(), "existing": False}).note("setup: no agent is free")
    for n, nxt in ((1, "sales-2"), (2, "sales-3"), (3, "sales-4")):
        s.advance(10000)
        s.deliver(n, overflow("OVERFLOW_ACTION_QUEUE", nxt)).note("setup: an overflow chain to the callback offer")
    s.advance(10000)
    s.deliver(4, overflow("OVERFLOW_ACTION_CALLBACK_OFFER")).note("L15: the entry keeps its place in sales-4")
    s.call("ConvertToCallback", {"owner": owner(4), "entry_id": E, "callback_number": NUMBER, "keep_position": True},
           {"callback_id": CB, "existing": False}).note("answered once RE's Postgres has the callback")
    s.call("ConvertToCallback", {"owner": owner(4), "entry_id": E, "callback_number": NUMBER, "keep_position": True},
           {"callback_id": CB, "existing": True}).note("a repeat (0.11.0)")
    s.call("Dequeue", {"owner": owner(4), "entry_id": E, "reason": "DEQUEUE_REASON_CALLBACK"},
           {"already_gone": True}).note("0.11.0: the entry already became the callback")
    out.append(s)

    s = Script("create-voicemail-task", "A voicemail left after an overflow becomes a task", ["coverage"], [],
               queues=[q("sales", max_wait_ms="30000", max_wait_action="OVERFLOW_ACTION_VOICEMAIL")])
    s.watch().advance(5000)
    s.call("Enqueue", note_enqueue(), {"entry": note_entry(), "existing": False}).note("setup")
    s.advance(30000)
    s.deliver(1, overflow("OVERFLOW_ACTION_VOICEMAIL")).note("setup: L13")
    s.call("Dequeue", {"owner": owner(1), "entry_id": E, "reason": "DEQUEUE_REASON_OVERFLOW"},
           {"already_gone": False}).note("setup: IE checkpointed the voicemail")
    s.advance(165000)
    vm = {"owner": owner(1), "queue_id": "sales", "recording_id": "0192f6a1-9300-7abc-8def-000000000009",
          "recorded_at_ms": s64(START + 200000), "duration_s": 42}
    s.call("CreateVoicemailTask", vm, {"task_id": TASK, "existing": False})
    s.call("CreateVoicemailTask", vm, {"task_id": TASK, "existing": True}).note("a repeat (0.11.0): the same task")
    out.append(s)

    s = Script("enqueue-consult", "A consult from the connected agent to a queue", ["coverage"],
               [dict(agent(T, A7), skills={"sales-en": 2})], queues=[q("sales"), q("billing")])
    s.watch().advance(5000)
    s.call("Enqueue", note_enqueue(), {"entry": note_entry(), "existing": False}).note("setup: the caller's queue")
    s.advance(25000)
    ready_and_accept(s)
    s.deliver(1, note_assigned())
    s.call("ReportOutcome", {"owner": owner(1), "reservation_id": R, "outcome": "OUTCOME_CONNECTED"},
           {"existing": False, "recorded": "OUTCOME_CONNECTED"}).note("setup: agent-7f3a is connected")
    s.advance(110000)
    consult = {"consult": {"from_agent_id": A7, "queue_id": "billing"}}
    b = note_enqueue(seq=2, queue="billing", ordering=START + 140000, reason="ENQUEUE_REASON_CONSULT")
    b["target"] = consult
    e4 = note_entry(entry_id=E4, queue="billing", ordering=START + 140000, seq=2)
    e4["target"] = consult
    s.call("Enqueue", b, {"entry": e4, "existing": False})
    out.append(s)

    s = Script("enqueue-direct-agent", "A direct offer to one agent", ["coverage"], [agent(T, A9)])
    s.watch().advance(5000)
    b = note_enqueue()
    b["target"] = {"agent_id": A9}
    e1 = note_entry()
    e1["target"] = {"agent_id": A9}
    s.call("Enqueue", b, {"entry": e1, "existing": False}).note("agent-9b1c is logged in, not ready: the call waits for it")
    out.append(s)

    s = Script("race-run-callback-resent",
               "A RunCallback is acknowledged right after the claim; a redelivery to another replica places no second call",
               ["F8", "N2"], [agent(T, A7)], streams=("r1", "r2"))
    s.doc["given"]["callback_deadline_ms"] = "3630000"
    s.watch().watch("r2").advance(5000)
    s.call("Enqueue", enqueue(at=START + 5000), {"entry": entry(at=START + 5000), "existing": False}).note("setup")
    s.call("ConvertToCallback", {"owner": owner(0), "entry_id": E, "callback_number": NUMBER, "keep_position": True},
           {"callback_id": CB, "existing": False}).note("setup: the caller asks for a callback and hangs up")
    s.advance(25000)
    ready_and_accept(s)
    run = {"run_callback": {
        "tenant_id": T, "interaction_id": IXC, "original_interaction_id": I, "callback_id": CB,
        "reservation_id": RC, "agent_id": A7, "agent_contact": contact(T, A7), "number": NUMBER,
        "queue_id": "sales", "first_queued_at_ms": s64(START + 5000), "deadline_ms": s64(START + 3660000)}}
    s.deliver(1, run).note("to ie-bin-a-1 (the caller's replica), once the attempt is durable")
    s.advance(5000)
    s.deliver(1, run, "r2").note(
        "to ie-bin-a-2: no ack within 5 s (sent before the ack arrived): its claim fails, or the interaction "
        "is in its ie.calls view; no call")
    s.call("Acknowledge", {"acks": [{"tenant_id": T, "interaction_id": IXC, "owner_epoch": "2001",
                                     "replica_id": R1, "acked_delivery_seq": "1"}]},
           {"ignored": 0}).note("sent right after ie-bin-a-1 claimed ie/owner/{interaction_id}")
    s.steps[-1]["metadata"] = {"traceparent": TP}
    s.call("ReportOutcome", {"owner": {"tenant_id": T, "interaction_id": IXC, "owner_epoch": "2001", "replica_id": R1,
                                       "acked_delivery_seq": "1"}, "reservation_id": RC, "outcome": "OUTCOME_CONNECTED"},
           {"existing": False, "recorded": "OUTCOME_CONNECTED"})
    out.append(s)

    s = Script("race-revoke-vs-connected", "A supervisor forces the agent's state just before CONNECTED",
               ["F1 L8", "F1 L9"], [agent(T, A7)])
    s.watch().advance(5000)
    s.call("Enqueue", enqueue(at=START + 5000), {"entry": entry(at=START + 5000), "existing": False}).note("setup")
    s.advance(25000)
    ready_and_accept(s)
    s.deliver(1, note_assigned())
    s.inject("supervisor_force", {"tenant_id": T, "agent_id": A7, "target": "TARGET_STATE_NOT_READY",
                                  "reason_code": "supervisor"}).note("0.11.0: agents.v1 ForceState")
    s.deliver(2, {"revoke": {"target": {"tenant_id": T, "interaction_id": I, "owner_epoch": s64(EP1)},
                             "reservation_id": R, "reason": "forced_state"}})
    s.call("ReportOutcome", {"owner": owner(1), "reservation_id": R, "outcome": "OUTCOME_CONNECTED"},
           {"existing": False, "recorded": "OUTCOME_REVOKED"})
    out.append(s)

    s = Script("race-stream-superseded",
               "The latest stream of a replica wins; unacknowledged messages are redelivered every 2 s; acks are batched",
               ["N3", "batched acks"], [agent(T, A7)], queues=[q("sales"), q("support")], streams=())
    s.doc["streams"] = {lbl: {"replica_id": R1, "bin": BIN} for lbl in ("s", "a", "b")}
    s.watch("s").advance(5000)
    s.call("Enqueue", enqueue(at=START + 5000), {"entry": entry(at=START + 5000), "existing": False}).note("setup")
    s.advance(25000)
    ready_and_accept(s)
    s.deliver(1, assigned(A7, START + 30000, reservation=R2), "s").note("setup")
    s.call("ReportOutcome", {"owner": owner(1), "reservation_id": R2, "outcome": "OUTCOME_AGENT_LEG_FAILED"},
           {"existing": False, "recorded": "OUTCOME_AGENT_LEG_FAILED"}).note("setup: L4, offered again")
    s.answers(T, A7, "DECISION_ACCEPT", "accepted")
    s.deliver(2, assigned(A7, START + 30000), "s").note("setup")
    s.call("Acknowledge", {"acks": [{"tenant_id": T, "interaction_id": I, "owner_epoch": s64(EP1), "replica_id": R1,
                                     "acked_delivery_seq": "2"}]}, {"ignored": 0}).note("setup")
    s.steps[-1]["metadata"] = {"traceparent": TP}
    s.advance(15000)
    s.call("Enqueue", enqueue(epoch=977, interaction=IX9, queue="support", at=START + 45000),
           {"entry": entry(entry_id=E2, queue="support", at=START + 45000), "existing": False}).note(
        "setup: another call of the replica")
    s.advance(16000)
    s.watch("a").note("stream A")
    s.watch("b").note("stream B, same replica_id: RE ends stream A with ABORTED / stream_superseded")
    s.inject("supervisor_force", {"tenant_id": T, "agent_id": A7, "target": "TARGET_STATE_LOGGED_OUT"}).note(
        "0.11.0: a supervisor logs the agent out (agents.v1 ForceState)")
    s.deliver(3, {"revoke": {"target": {"tenant_id": T, "interaction_id": I, "owner_epoch": s64(EP1)},
                             "reservation_id": R, "reason": "agent_logged_out"}}, "b").note("on stream B")
    s.advance(2000)
    s.deliver(3, {"revoke": {"target": {"tenant_id": T, "interaction_id": I, "owner_epoch": s64(EP1)},
                             "reservation_id": R, "reason": "agent_logged_out"}}, "b").note(
        "redelivered after 2 s without an ack")
    s.call("Acknowledge", {"acks": [{"tenant_id": T, "interaction_id": I, "owner_epoch": s64(EP1), "replica_id": R1,
                                     "acked_delivery_seq": "3"},
                                    {"tenant_id": T, "interaction_id": IX9, "owner_epoch": "977", "replica_id": R1,
                                     "acked_delivery_seq": "1"}]}, {"ignored": 0})
    s.steps[-1]["metadata"] = {"traceparent": TP}
    out.append(s)

    s = Script("reject-skip-is-temporary",
               "0.11.0: one agent, one queue: a reject never strands the caller (re-offered at once: no other agent is "
               "ready), and the call still leaves by its max-wait action", ["PLAN 4.2"],
               [agent(T, A7, ready_since=START)],
               queues=[q("sales", max_wait_ms="60000", max_wait_action="OVERFLOW_ACTION_DISCONNECT")])
    s.watch()
    e = entry()
    e["expected_wait_quality"] = "EXPECTED_WAIT_QUALITY_HEURISTIC"
    s.call("Enqueue", enqueue(), {"entry": e, "existing": False})
    s.answers(T, A7, "DECISION_REJECT", "rejected")
    s.answers(T, A7, "DECISION_REJECT", "rejected").note("offered again at once")
    s.advance(60000)
    s.deliver(1, {"overflow": {"target": {"tenant_id": T, "interaction_id": I, "owner_epoch": s64(EP1)},
                               "entry_id": E, "action": "OVERFLOW_ACTION_DISCONNECT"}})
    s.call("Dequeue", {"owner": owner(1), "entry_id": E, "reason": "DEQUEUE_REASON_OVERFLOW"}, {"already_gone": False})
    out.append(s)

    s = Script("max-wait-action-fires", "0.11.0: no agent is ever ready: the queue's max-wait action at max_wait_s",
               ["PLAN 4.2", "L13"], [],
               queues=[q("sales", max_wait_ms="30000", max_wait_action="OVERFLOW_ACTION_FLOW_STEP",
                         max_wait_flow_step="sorry-closed")])
    s.watch()
    e = entry()
    e["expected_wait_quality"] = "EXPECTED_WAIT_QUALITY_LONG"
    s.call("Enqueue", enqueue(), {"entry": e, "existing": False}).note(
        "no agent logged in: LONG, expected_wait_s a lower bound (the time to max wait)")
    s.advance(30000)
    s.deliver(1, {"overflow": {"target": {"tenant_id": T, "interaction_id": I, "owner_epoch": s64(EP1)},
                               "entry_id": E, "action": "OVERFLOW_ACTION_FLOW_STEP", "flow_step": "sorry-closed"}})
    s.call("Dequeue", {"owner": owner(1), "entry_id": E, "reason": "DEQUEUE_REASON_OVERFLOW"}, {"already_gone": False})
    s.call("Dequeue", {"owner": owner(1), "entry_id": E, "reason": "DEQUEUE_REASON_OVERFLOW"}, {"already_gone": True})
    out.append(s)
    return out

# ---- 0.14.0: campaign lending (IE step 13a, parity-cc-routing phase 8) ----
CAMPAIGN = "spring-renewals"
EPC1, EPC2 = 2051, 2061                      # campaign leaders' fences (etcd create revisions)
IC = "0192f6d0-3a1b-7c2d-8e3f-405162738495"  # a campaign call (IE made it)
EPIC = 1082                                  # its owner's fence
RCAMP = "0192f6d0-9100-7ccc-8ddd-000000000001"  # its reservation (placeholder)
B2 = "agent-2b9c"
C5 = "agent-5d1e"
GA = "agent-9e8d"                            # a globex agent


def cowner(epoch=EPC1, replica=R1, tenant=T):
    return {"tenant_id": tenant, "campaign_id": CAMPAIGN, "owner_epoch": s64(epoch), "replica_id": replica}


def avail(agent_id, state, epoch=EPC1, wrap_ends=None):
    a = {"tenant_id": T, "agent_id": agent_id, "campaign_id": CAMPAIGN, "state": "AVAILABILITY_STATE_" + state,
         "owner_epoch": s64(epoch)}
    if wrap_ends is not None:
        a["wrapup_ends_at_ms"] = s64(wrap_ends)
    return a


def lent(agent_id, result):
    return {"agent_id": agent_id, "result": "LEND_RESULT_" + result}


def skilled(tenant, agent_id, ready_since=None, skills=None, queues=("sales",)):
    a = agent(tenant, agent_id, ready_since, queues)
    if skills:
        a["skills"] = skills
    return a


def campaign():
    """0.14.0: LendAgents, ReturnAgents, ReserveAgent and the AgentAvailability Watch case."""
    out = []

    s = Script("campaign-agent-no-inbound",
               "0.14.0: a lent agent gets no inbound offer, though it has idled longest; returned, it takes the next "
               "call as a primary again (no dwell owed)", ["PLAN 4.3 mode", "IE PLAN campaign dialing"],
               [agent(T, A7, ready_since=START - 20000), agent(T, B2, ready_since=START - 10000)])
    s.watch()
    s.call("LendAgents", {"owner": cowner(), "agent_ids": [A7]},
           {"agents": [lent(A7, "LENT")], "lent": [avail(A7, "READY")], "existing": False}).note(
        "idle and READY: the switch is at once; agent-2b9c still covers sales")
    s.deliver(0, {"agent_availability": avail(A7, "READY")}).note("to the leader's stream; delivery_seq 0, never replayed")
    s.advance(1000)
    s.call("Enqueue", enqueue(at=START + 1000), {"entry": entry(at=START + 1000), "existing": False})
    s.answers(T, A7, "DECISION_ACCEPT", "no_offer").note("lent: never offered, though it has idled longest")
    s.answers(T, B2, "DECISION_ACCEPT", "accepted")
    s.deliver(1, assigned(B2, START + 1000))
    s.call("ReturnAgents", {"owner": cowner(), "agent_ids": [A7]},
           {"agents": [{"agent_id": A7, "result": "RETURN_RESULT_RETURNED"}]}).note("idle: back to inbound at once")
    s.deliver(0, {"agent_availability": avail(A7, "RETURNED")})
    s.advance(1000)
    s.call("Enqueue", enqueue(epoch=EP2, interaction=I2, at=START + 2000),
           {"entry": entry(entry_id=E2, at=START + 2000), "existing": False})
    s.answers(T, A7, "DECISION_ACCEPT", "accepted").note("inbound again: it takes the next call")
    s.deliver(1, assigned(A7, START + 2000, reservation=R2, entry_id=E2, epoch=EP2, interaction=I2))
    out.append(s)

    s = Script("campaign-lend-pending-after-wrap-up",
               "0.14.0: an agent on an inbound call is lent only once its wrap-up ends (a pending switch, reported); "
               "until then it can't be reserved for the campaign", ["PLAN 4.3 mode", "IE PLAN campaign dialing"],
               [agent(T, A7, ready_since=START - 20000), agent(T, B2)])
    s.watch()
    s.call("Enqueue", enqueue(), {"entry": entry(), "existing": False})
    s.answers(T, A7, "DECISION_ACCEPT", "accepted")
    s.deliver(1, assigned(A7, START))
    s.call("ReportOutcome", {"owner": owner(1), "reservation_id": R, "outcome": "OUTCOME_CONNECTED"},
           {"existing": False, "recorded": "OUTCOME_CONNECTED"})
    s.call("LendAgents", {"owner": cowner(), "agent_ids": [A7]},
           {"agents": [lent(A7, "PENDING")], "lent": [avail(A7, "PENDING")], "existing": False}).note(
        "on a call: the switch waits for its wrap-up to end; agent-2b9c (not ready, logged in) still covers sales")
    s.deliver(0, {"agent_availability": avail(A7, "PENDING")})
    s.call("ReserveAgent", {"owner": owner(0, EPIC, IC), "campaign": cowner(), "agent_id": A7},
           err=("FAILED_PRECONDITION", "agent_not_lent")).note("pending is not lent yet")
    s.advance(30000)
    s.call("CallEnded", {"owner": owner(1), "agent_id": A7, "reservation_id": R, "ended_at_ms": s64(START + 30000)},
           {"existing": False})
    s.advance(60000).note("the wrap-up limit (60 s)")
    s.deliver(2, {"wrap_up_ended": {"target": {"tenant_id": T, "interaction_id": I, "owner_epoch": s64(EP1)},
                                    "reservation_id": R, "agent_id": A7, "ended_at_ms": s64(START + 90000)}})
    s.deliver(0, {"agent_availability": avail(A7, "READY")}).note("wrap-up over: the switch happens, the agent is lent")
    s.call("Acknowledge", {"acks": [{"tenant_id": T, "interaction_id": I, "owner_epoch": s64(EP1), "replica_id": R1,
                                     "acked_delivery_seq": "2"}]}, {"ignored": 0})
    s.steps[-1]["metadata"] = {"traceparent": TP}
    s.call("LendAgents", {"owner": cowner(), "agent_ids": [A7]},
           {"agents": [lent(A7, "LENT")], "lent": [avail(A7, "READY")], "existing": False}).note(
        "a repeat: already lent to this campaign")
    out.append(s)

    s = Script("campaign-reserve-agent",
               "0.14.0: the leader reserves a lent agent for its campaign call; ReportOutcome and CallEnded run the "
               "reservation as an inbound one; AgentAvailability follows the agent through its wrap-up",
               ["IE PLAN campaign dialing (progressive)"], [agent(T, A7, ready_since=START - 20000), agent(T, B2)])
    s.watch()
    s.call("LendAgents", {"owner": cowner(), "agent_ids": [A7]},
           {"agents": [lent(A7, "LENT")], "lent": [avail(A7, "READY")], "existing": False})
    s.deliver(0, {"agent_availability": avail(A7, "READY")})
    s.advance(1000)
    res = {"reservation_id": RCAMP, "agent_id": A7, "agent_contact": contact(T, A7),
           "deadline_ms": s64(START + 1000 + DEADLINE), "state": "RESERVATION_STATE_ASSIGNED"}
    s.call("ReserveAgent", {"owner": owner(0, EPIC, IC), "campaign": cowner(), "agent_id": A7},
           {"reservation": res, "existing": False}).note("before the dial: an answered call never waits")
    s.deliver(0, {"agent_availability": avail(A7, "RESERVED")})
    s.call("ReserveAgent", {"owner": owner(0, EPIC, IC), "campaign": cowner(), "agent_id": A7},
           {"reservation": res, "existing": True}).note("a retry: the same reservation")
    s.advance(9000)
    s.call("ReportOutcome", {"owner": owner(0, EPIC, IC), "reservation_id": RCAMP, "outcome": "OUTCOME_CONNECTED"},
           {"existing": False, "recorded": "OUTCOME_CONNECTED"}).note("the customer answered")
    s.deliver(0, {"agent_availability": avail(A7, "ON_CALL")})
    s.advance(120000)
    s.call("CallEnded", {"owner": owner(0, EPIC, IC), "agent_id": A7, "reservation_id": RCAMP,
                         "ended_at_ms": s64(START + 130000)}, {"existing": False})
    s.deliver(0, {"agent_availability": avail(A7, "WRAP_UP", wrap_ends=START + 190000)})
    s.advance(60000).note("the wrap-up limit (60 s)")
    s.deliver(1, {"wrap_up_ended": {"target": {"tenant_id": T, "interaction_id": IC, "owner_epoch": s64(EPIC)},
                                    "reservation_id": RCAMP, "agent_id": A7, "ended_at_ms": s64(START + 190000)}})
    s.deliver(0, {"agent_availability": avail(A7, "READY")}).note("lent and idle again: the leader may reserve it")
    s.call("ReturnAgents", {"owner": cowner(), "agent_ids": []},
           {"agents": [{"agent_id": A7, "result": "RETURN_RESULT_RETURNED"}]}).note("the campaign ended: all back")
    s.deliver(0, {"agent_availability": avail(A7, "RETURNED")})
    out.append(s)

    s = Script("campaign-lend-refusals",
               "0.14.0: lending is refused, not deferred, when it would leave an open queue uncovered; another "
               "tenant's agent is unknown; an agent that isn't lent can't be reserved",
               ["PLAN 4.3 coverage", "PLAN 4.3 tenant isolation"],
               [agent(T, A7, ready_since=START - 20000), agent(T2, GA, ready_since=START - 20000)], tenants=(T, T2))
    s.watch()
    s.call("LendAgents", {"owner": cowner(), "agent_ids": [A7, GA, "agent-0000"]},
           {"agents": [lent(A7, "COVERAGE"), lent(GA, "UNKNOWN_AGENT"), lent("agent-0000", "UNKNOWN_AGENT")],
            "existing": False}).note("agent-7f3a is sales' only agent; agent-9e8d is globex's")
    s.call("ReserveAgent", {"owner": owner(0, EPIC, IC), "campaign": cowner(), "agent_id": A7},
           err=("FAILED_PRECONDITION", "agent_not_lent"))
    out.append(s)

    s = Script("campaign-lend-by-count",
               "0.14.0: RE picks the agents: idle READY ones with the skills, longest idle first, never the last one "
               "covering a queue; a repeat of request_id returns the first answer",
               ["IE PLAN campaign dialing", "PLAN 4.3 coverage"],
               [skilled(T, A7, START - 30000, {"outbound-en": 2}), skilled(T, B2, START - 20000, {"outbound-en": 1}),
                skilled(T, C5, START - 10000, {"outbound-en": 1})])
    s.watch()
    lend = {"owner": cowner(), "count": 3, "requirements": [{"skill_id": "outbound-en", "min_level": 1}],
            "request_id": "lend-spring-1"}
    s.call("LendAgents", lend,
           {"agents": [lent(A7, "LENT"), lent(B2, "LENT")], "lent": [avail(B2, "READY"), avail(A7, "READY")],
            "existing": False}).note("three asked, two lent: agent-5d1e is left covering sales")
    s.deliver(0, {"agent_availability": avail(A7, "READY")})
    s.deliver(0, {"agent_availability": avail(B2, "READY")})
    s.call("LendAgents", lend,
           {"agents": [lent(A7, "LENT"), lent(B2, "LENT")], "lent": [avail(B2, "READY"), avail(A7, "READY")],
            "existing": True}).note("a retry of the same request: nothing more is lent")
    out.append(s)

    s = Script("race-campaign-leader-takeover",
               "0.14.0: a new campaign leader (a higher fence, another replica) takes the campaign over: it gets the "
               "lent agents and their availability on its stream; the old leader is refused",
               ["IE PLAN campaign dialing (leader)"], [agent(T, A7, ready_since=START - 20000), agent(T, B2)],
               streams=("r1", "r2"))
    s.watch().watch("r2")
    s.call("LendAgents", {"owner": cowner(), "agent_ids": [A7]},
           {"agents": [lent(A7, "LENT")], "lent": [avail(A7, "READY")], "existing": False})
    s.deliver(0, {"agent_availability": avail(A7, "READY")})
    s.call("LendAgents", {"owner": cowner(EPC2, R2_REPLICA), "agent_ids": []},
           {"lent": [avail(A7, "READY", EPC2)], "existing": False}).note("ie-bin-a-1 died; ie-bin-a-2 won the election")
    s.deliver(0, {"agent_availability": avail(A7, "READY", EPC2)}, "r2").note("the campaign's agents, to the new leader")
    s.call("ReturnAgents", {"owner": cowner(), "agent_ids": [A7]}, err=("ABORTED", "stale_owner")).note(
        "the old leader's late call")
    s.call("ReturnAgents", {"owner": cowner(EPC2, R2_REPLICA), "agent_ids": [A7]},
           {"agents": [{"agent_id": A7, "result": "RETURN_RESULT_RETURNED"}]})
    out.append(s)
    return out


def main():
    root = pathlib.Path(__file__).resolve().parent.parent
    for s in scripts() + notes() + phase34() + campaign():
        print(s.write(root).relative_to(root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
