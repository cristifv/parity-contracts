#!/usr/bin/env python3
"""The learned forecaster's training catalogue generator and the OOD suite's leakage check
(parity-cc-routing PLAN §3.8, §5.9; the user's design, 2026-11-01). Stdlib only.

  forecast_scenarios.py expand  --catalogue FILE --out DIR [--samples N] [--holdout K] [--seed S]
      Expands the compound catalogue (routing-scenario.v1 with forecast.purpose training_catalogue)
      into concrete training_sample files: each draws a tenant (queue count, volumes, shapes) and its
      days within the catalogue's ranges (domain randomization), with the seed and every drawn
      parameter recorded in forecast.sample, plus manifest.json (seeds, train/holdout split).
      Sample i uses seed base + i; the last K samples are the holdout. Same seed, same file.
  forecast_scenarios.py leakage --catalogue FILE --ood DIR [--samples N]
      The OOD suite must stay out of training: every OOD feature lies outside the catalogue's
      leakage_bounds (or names a feature the catalogue never produces), the bounds hold for N
      expanded samples (the generator can't drift past them), and no OOD file shares a name or its
      arrivals with a sample.
  forecast_scenarios.py check   --catalogue FILE
      Every pattern key is known and every range is [min <= max].

Patterns (forecast.patterns; every range is [min, max], drawn uniformly unless noted):
  tenant          queues [n], peak_per_hour (log-uniform), the shared test tenant id
  days            sample length in days
  start_day       epoch day range of t = 0 (any weekday)
  day_level       quiet/normal/busy: weight and level range (per day)
  intraday        kinds with weights (weekday, lunch_dip, end_of_day, flat, evening), open_h,
                  close_h, night_level, tz_offset_h (local time = UTC + offset)
  weekly          weekend_level (Saturday; Sunday gets 0.6 of it)
  holidays        per_sample (count), level
  campaign_surge  per_week, multiplier, duration_h, ramp_min (a known event when event_known)
  spike           per_week, multiplier (3-10x), duration_min, ramp_s, decay_s, min_gap_h
  outage          per_week, duration_min, level, recovery_multiplier, recovery_min
  shift_start     probability (per queue), multiplier, duration_min (daily at opening)
  trend           per_week (multiplicative)
  drift           level_step probability, multiplier, ramp_h; aht_per_week
  redial          p, mean_delay_s
  overdispersion  cv
  bursts          per_hour, mean_size
  handle_time     mean_s (log-uniform), cv
  patience        mean_s
  staffing        occupancy, factor
  quirks          dst (probability), gap per_week + minutes, duplicate per_week + minutes,
                  rename (probability), merge (probability; two queues)
  flags           holiday_known, event_known (probability the forecaster is told)
"""
import argparse
import hashlib
import json
import math
import os
import random
import sys

DAY = 86400
WEEK = 7 * DAY
KNOWN = {"tenant", "days", "start_day", "day_level", "intraday", "weekly", "holidays", "campaign_surge", "spike",
         "outage", "shift_start", "trend", "drift", "redial", "overdispersion", "bursts", "handle_time", "patience",
         "staffing", "quirks", "flags"}


def die(msg):
    print(f"forecast_scenarios: {msg}", file=sys.stderr)
    sys.exit(2)


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def uni(r, rng):
    return r[0] + (r[1] - r[0]) * rng.random()


def log_uni(r, rng):
    return math.exp(uni([math.log(r[0]), math.log(r[1])], rng))


def iuni(r, rng):
    return rng.randint(int(r[0]), int(r[1]))


def count(per_week, days, rng):
    """A Poisson count for the sample's length from a per-week rate drawn in its range."""
    lam = uni(per_week, rng) * days / 7.0
    n, p, l = 0, rng.random(), math.exp(-lam)
    while p > l:
        n += 1
        p *= rng.random()
    return n


def shape(kind, h, open_h, close_h, night):
    """The relative rate at local hour h (peak ~1)."""
    def sig(x):
        return 1 / (1 + math.exp(-x))
    open_mask = sig((h - open_h) * 3) * sig((close_h - h) * 3)
    mid = (open_h + close_h) / 2
    if kind == "weekday":
        form = 0.75 + 0.25 * math.exp(-((h - (open_h + 2.5)) ** 2) / 4) + 0.2 * math.exp(-((h - (mid + 2)) ** 2) / 5)
    elif kind == "lunch_dip":
        form = 0.95 - 0.4 * math.exp(-((h - 12.5) ** 2) / 0.8)
    elif kind == "end_of_day":
        form = 0.55 + 0.45 * sig((h - (close_h - 2.5)) * 2) * sig((close_h - 0.3 - h) * 6)
    elif kind == "evening":
        form = 0.45 + 0.55 * math.exp(-((h - 19.5) ** 2) / 4)
        open_mask = sig((h - open_h) * 3) * sig((max(close_h, 22) - h) * 3)
    else:  # flat
        form = 1.0
    return night + (1 - night) * open_mask * min(1.0, form)


def pick(weights, rng):
    keys = sorted(weights)
    tot = sum(weights[k] for k in keys)
    x = rng.random() * tot
    for k in keys:
        x -= weights[k]
        if x <= 0:
            return k
    return keys[-1]


def expand_one(cat, i, seed):
    pat = cat["forecast"]["patterns"]
    rng = random.Random(seed)
    days = iuni(pat["days"], rng)
    dur = days * DAY
    start_day = iuni(pat["start_day"], rng)
    start_ms = start_day * DAY * 1000
    nq = iuni(pat["tenant"]["queues"], rng)
    tz = iuni(pat["intraday"]["tz_offset_h"], rng)
    kind = pick(pat["intraday"]["kinds"], rng)
    open_h = uni(pat["intraday"]["open_h"], rng)
    close_h = uni(pat["intraday"]["close_h"], rng)
    night = uni(pat["intraday"]["night_level"], rng)
    weekend = uni(pat["weekly"]["weekend_level"], rng)
    trend = uni(pat["trend"]["per_week"], rng)
    lv = pat["day_level"]
    day_mult = []
    for _ in range(days):
        k = pick({x: lv[x]["weight"] for x in ("quiet", "normal", "busy")}, rng)
        day_mult.append(uni(lv[k]["level"], rng))
    hol = []
    for _ in range(iuni(pat["holidays"]["per_sample"], rng)):
        d = rng.randrange(days)
        hol.append({"day": d, "level": uni(pat["holidays"]["level"], rng)})
    # DST: the local offset moves by an hour from some day on.
    dst = None
    if rng.random() < pat["quirks"]["dst"]:
        dst = {"day": rng.randrange(1, days), "shift_h": rng.choice([-1, 1])}
    surges = []
    for _ in range(count(pat["campaign_surge"]["per_week"], days, rng)):
        st = rng.uniform(0, dur - 3600)
        surges.append({"start_s": st, "duration_s": uni(pat["campaign_surge"]["duration_h"], rng) * 3600,
                       "multiplier": uni(pat["campaign_surge"]["multiplier"], rng),
                       "ramp_s": uni(pat["campaign_surge"]["ramp_min"], rng) * 60})
    flags_hol = rng.random() < pat["flags"]["holiday_known"]
    flags_ev = rng.random() < pat["flags"]["event_known"]
    level_step = None
    if rng.random() < pat["drift"]["level_step"]["probability"]:
        level_step = {"at_s": round(rng.uniform(DAY, dur - DAY), 1), "multiplier": round(uni(pat["drift"]["level_step"]["multiplier"], rng), 4),
                      "ramp_s": round(uni(pat["drift"]["level_step"]["ramp_h"], rng) * 3600, 1)}
    occupancy = uni(pat["staffing"]["occupancy"], rng)
    factor = uni(pat["staffing"]["factor"], rng)
    aht_drift = uni(pat["drift"]["aht_per_week"], rng)

    def local_h(t):
        off = tz + (dst["shift_h"] if dst and t >= dst["day"] * DAY else 0)
        return ((start_ms / 1000 + t) / 3600 + off) % 24

    def weekday(t):
        return int(((start_ms / 1000 + t) // DAY + 3) % 7)  # 1970-01-01 was a Thursday; Monday = 0

    queues = []
    faults = []
    for q in range(nq):
        qid = f"q{q + 1}"
        peak = log_uni(pat["tenant"]["peak_per_hour"], rng)
        qkind = kind if rng.random() < 0.7 else pick(pat["intraday"]["kinds"], rng)
        pts = []
        for k in range(days * 96 + 1):
            t = k * 900
            d = min(days - 1, int(t // DAY))
            wd = weekday(t)
            m = day_mult[d] * (1 if wd < 5 else weekend if wd == 5 else 0.6 * weekend)
            for hd in hol:
                if hd["day"] == d:
                    m *= hd["level"]
            m *= math.exp(math.log1p(trend) * t / WEEK)
            for s in surges:
                a, b, r = s["start_s"], s["start_s"] + s["duration_s"], max(1.0, s["ramp_s"])
                if a <= t < b + r:
                    f = min(1.0, (t - a) / r, max(0.0, (b + r - t) / r))
                    m *= 1 + (s["multiplier"] - 1) * f
            pts.append({"t_s": t, "per_hour": round(peak * m * shape(qkind, local_h(t), open_h, close_h, night), 4)})
        spikes = []
        # 3-10x spikes, at least min_gap_h apart.
        taken = []
        for _ in range(count(pat["spike"]["per_week"], days, rng)):
            st = rng.uniform(0, dur - 3600)
            if any(abs(st - x) < pat["spike"]["min_gap_h"] * 3600 for x in taken):
                continue
            taken.append(st)
            spikes.append({"start_s": round(st, 1), "duration_s": round(uni(pat["spike"]["duration_min"], rng) * 60, 1),
                           "multiplier": round(uni(pat["spike"]["multiplier"], rng), 3), "ramp_s": round(uni(pat["spike"]["ramp_s"], rng), 1),
                           "decay_s": round(uni(pat["spike"]["decay_s"], rng), 1)})
        for _ in range(count(pat["outage"]["per_week"], days, rng)):
            st = rng.uniform(0, dur - 7200)
            od = uni(pat["outage"]["duration_min"], rng) * 60
            spikes.append({"start_s": round(st, 1), "duration_s": round(od, 1), "multiplier": round(uni(pat["outage"]["level"], rng), 3), "ramp_s": 5})
            spikes.append({"start_s": round(st + od, 1), "duration_s": 60, "multiplier": round(uni(pat["outage"]["recovery_multiplier"], rng), 3),
                           "ramp_s": 5, "decay_s": round(uni(pat["outage"]["recovery_min"], rng) * 60, 1)})
        if rng.random() < pat["shift_start"]["probability"]:
            mult = uni(pat["shift_start"]["multiplier"], rng)
            dm = uni(pat["shift_start"]["duration_min"], rng) * 60
            for d in range(days):
                st = d * DAY + ((open_h - tz) % 24) * 3600
                if 0 <= st < dur - dm and weekday(st) < 5:
                    spikes.append({"start_s": round(st, 1), "duration_s": round(dm, 1), "multiplier": round(mult, 3), "ramp_s": 120})
        spikes.sort(key=lambda s: s["start_s"])
        arr = {"kind": "poisson", "points": pts, "spikes": spikes,
               "overdispersion": round(uni(pat["overdispersion"]["cv"], rng), 3),
               "bursts": {"per_hour": round(uni(pat["bursts"]["per_hour"], rng), 3), "mean_size": round(uni(pat["bursts"]["mean_size"], rng), 2)}}
        if level_step and (q == 0 or rng.random() < 0.5):
            arr["level_steps"] = [level_step]
        queues.append({
            "id": qid, "max_wait_s": 600, "max_wait_action": {"action": "disconnect"}, "arrivals": arr,
            "handle_time": {"kind": "lognormal", "mean_s": round(log_uni(pat["handle_time"]["mean_s"], rng), 1), "cv": round(uni(pat["handle_time"]["cv"], rng), 3)},
            "patience": {"kind": "exponential", "mean_s": round(uni(pat["patience"]["mean_s"], rng), 1)},
            "redial": {"p": round(uni(pat["redial"]["p"], rng), 3), "mean_delay_s": round(uni(pat["redial"]["mean_delay_s"], rng), 1)},
        })
        for _ in range(count(pat["quirks"]["gap"]["per_week"], days, rng)):
            faults.append({"kind": "gap", "at_s": round(rng.uniform(0, dur - 600), 1), "duration_s": round(uni(pat["quirks"]["gap"]["minutes"], rng) * 60, 1), "queue": qid})
        for _ in range(count(pat["quirks"]["duplicate"]["per_week"], days, rng)):
            faults.append({"kind": "duplicate", "at_s": round(rng.uniform(0, dur - 600), 1), "duration_s": round(uni(pat["quirks"]["duplicate"]["minutes"], rng) * 60, 1), "queue": qid})
    if nq >= 1 and rng.random() < pat["quirks"]["rename"]:
        faults.append({"kind": "rename", "at_s": round(rng.uniform(DAY, dur - DAY / 2), 1), "queue": "q1", "to_queue": "q1-renamed"})
    if nq >= 2 and rng.random() < pat["quirks"]["merge"]:
        faults.append({"kind": "merge", "at_s": round(rng.uniform(DAY, dur - DAY / 2), 1), "queues": ["q1", "q2"], "to_queue": "q1"})
    if dst:
        faults.append({"kind": "dst", "at_s": dst["day"] * DAY, "jump_s": dst["shift_h"] * 3600})
    cal = {"holidays": [{"start_s": h["day"] * DAY, "end_s": (h["day"] + 1) * DAY, "known": flags_hol} for h in hol],
           "events": [{"start_s": round(s["start_s"], 1), "end_s": round(s["start_s"] + s["duration_s"], 1), "known": flags_ev} for s in surges]}
    name = f"fc-sample-{i:05d}"
    return {
        "schema_version": 1, "name": name,
        "description": f"Forecaster training sample {i} expanded from {cat['name']} (seed {seed}); test data only",
        "seed": seed, "duration_s": dur, "bin": "bin-a",
        "tenants": [{"id": cat["tenants"][0]["id"], "queues": queues,
                     "agents": [{"count": 1, "id_prefix": "fc-agent-", "queues": [q["id"] for q in queues]}]}],
        "expectations": {"invariants": "all"},
        "forecast": {"purpose": "training_sample", "start_ms": start_ms, "resolutions_s": [10, 60, 900],
                     "staffing": {"occupancy": round(occupancy, 4), "factor": round(factor, 4)},
                     "calendar": cal, "data_faults": faults,
                     "sample": {"index": i, "seed": seed, "catalogue": cat["name"], "days": days, "queues": nq,
                                "intraday": kind, "open_h": round(open_h, 3), "close_h": round(close_h, 3), "night": round(night, 4),
                                "tz_offset_h": tz, "weekend_level": round(weekend, 4), "trend_per_week": round(trend, 5),
                                "day_levels": [round(x, 4) for x in day_mult], "holidays": hol, "dst": dst,
                                "level_step": level_step, "aht_drift_per_week": round(aht_drift, 5),
                                "holiday_known": flags_hol, "event_known": flags_ev}},
    }


def features(doc):
    """The OOD-relevant features a scenario contains (the leakage check's vocabulary)."""
    f = {}
    def put(k, v):
        f.setdefault(k, []).append(v)
    for t in doc["tenants"]:
        for q in t["queues"]:
            a = q["arrivals"]
            sp = sorted(a.get("spikes", []), key=lambda s: s["start_s"])
            for s in sp:
                if s["multiplier"] >= 3:
                    put("spike_multiplier", s["multiplier"])
                if s["multiplier"] < 1:
                    put("outage_minutes", s["duration_s"] / 60)
            big = [s for s in sp if s["multiplier"] >= 3]
            for x, y in zip(big, big[1:]):
                put("spike_gap_min", (y["start_s"] - x["start_s"]) / 60)
            for s in a.get("level_steps", []):
                put("level_step_multiplier", s["multiplier"])
            for b in a.get("bots", []):
                put("bots_per_hour", b["per_hour"])
            if "redial" in q:
                put("redial_p", q["redial"]["p"])
    for d in doc.get("forecast", {}).get("data_faults", []):
        if d["kind"] == "gap":
            put("gap_minutes", d.get("duration_s", 0) / 60)
        elif d["kind"] == "duplicate":
            put("duplicate_minutes", d.get("duration_s", 0) / 60)
        elif d["kind"] == "clock_jump":
            put("clock_jump_s", abs(d.get("jump_s", 0)))
        elif d["kind"] == "merge":
            put("merge_queues", len(d.get("queues", [])))
    return f


def arrivals_digest(doc):
    return hashlib.sha256(json.dumps([q["arrivals"] for t in doc["tenants"] for q in t["queues"]], sort_keys=True).encode()).hexdigest()


def cmd_check(cat):
    if cat.get("forecast", {}).get("purpose") != "training_catalogue":
        die("not a training catalogue")
    pat = cat["forecast"]["patterns"]
    unknown = set(pat) - KNOWN
    missing = KNOWN - set(pat)
    if unknown or missing:
        die(f"patterns: unknown {sorted(unknown)}, missing {sorted(missing)}")

    def walk(o, path):
        if isinstance(o, list) and len(o) == 2 and all(isinstance(x, (int, float)) for x in o):
            if o[0] > o[1]:
                die(f"{path}: min > max")
        elif isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{path}.{k}")
    walk(pat, "patterns")
    return 0


def cmd_expand(a):
    cat = load(a.catalogue)
    cmd_check(cat)
    seeds = cat["forecast"].get("seeds", {})
    base = a.seed if a.seed is not None else seeds.get("base", 1)
    n = a.samples or seeds.get("samples", 100)
    k = a.holdout if a.holdout is not None else seeds.get("holdout", 0)
    os.makedirs(a.out, exist_ok=True)
    man = {"catalogue": cat["name"], "base_seed": base, "samples": n, "holdout": k, "files": []}
    for i in range(n):
        doc = expand_one(cat, i, base + i)
        p = os.path.join(a.out, doc["name"] + ".json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(doc, f, separators=(",", ":"))
        man["files"].append({"name": doc["name"], "seed": base + i, "split": "holdout" if i >= n - k else "train"})
    with open(os.path.join(a.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, indent=1)
    print(f"expanded {n} samples ({k} holdout) from seed {base} into {a.out}")
    return 0


def cmd_leakage(a):
    cat = load(a.catalogue)
    cmd_check(cat)
    bounds = cat["forecast"]["leakage_bounds"]
    errs = []
    seeds = cat["forecast"].get("seeds", {})
    base = seeds.get("base", 1)
    digests = {}
    for i in range(a.samples):
        doc = expand_one(cat, i, base + i)
        digests[arrivals_digest(doc)] = doc["name"]
        for k, vs in features(doc).items():
            if k not in bounds:
                errs.append(f"{doc['name']}: produces feature {k} with no leakage bound")
                continue
            lo, hi = bounds[k]
            for v in vs:
                if not (lo - 1e-9 <= v <= hi + 1e-9):
                    errs.append(f"{doc['name']}: {k} = {v} outside its own bound {bounds[k]}")
    files = sorted(x for x in os.listdir(a.ood) if x.endswith(".json"))
    if not files:
        errs.append("no OOD scenarios")
    for x in files:
        doc = load(os.path.join(a.ood, x))
        if doc.get("forecast", {}).get("purpose") != "ood":
            errs.append(f"{x}: forecast.purpose is not ood")
            continue
        if doc["name"].startswith("fc-sample-"):
            errs.append(f"{x}: named like a training sample")
        if arrivals_digest(doc) in digests:
            errs.append(f"{x}: same arrivals as {digests[arrivals_digest(doc)]}")
        inside = []
        for feat in doc["forecast"]["ood"]["features"]:
            k, v = feat["name"], feat["value"]
            if k in bounds and bounds[k][0] - 1e-9 <= v <= bounds[k][1] + 1e-9:
                inside.append(f"{k}={v} within {bounds[k]}")
        if inside:
            errs.append(f"{x}: OOD feature inside the training catalogue: {', '.join(inside)}")
    for e in errs:
        print("LEAK: " + e, file=sys.stderr)
    if errs:
        return 1
    print(f"leakage check: {len(files)} OOD scenarios outside the catalogue; bounds hold for {a.samples} samples")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("expand")
    e.add_argument("--catalogue", required=True)
    e.add_argument("--out", required=True)
    e.add_argument("--samples", type=int)
    e.add_argument("--holdout", type=int)
    e.add_argument("--seed", type=int)
    lk = sub.add_parser("leakage")
    lk.add_argument("--catalogue", required=True)
    lk.add_argument("--ood", required=True)
    lk.add_argument("--samples", type=int, default=60)
    c = sub.add_parser("check")
    c.add_argument("--catalogue", required=True)
    a = ap.parse_args()
    if a.cmd == "expand":
        return cmd_expand(a)
    if a.cmd == "leakage":
        return cmd_leakage(a)
    return cmd_check(load(a.catalogue))


if __name__ == "__main__":
    sys.exit(main())
