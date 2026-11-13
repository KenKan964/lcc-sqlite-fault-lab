"""Minimal LCC plan interpreter, conservative resolver and evidence-based checks."""
from __future__ import annotations
from collections import Counter
from adapter import payload_hash


def resolve(lab, amount=7):
    observation = lab.observe()
    if any(c.p.poll() is None for c in lab.children.values()):
        state = "HOLD_IN_FLIGHT"
    elif not observation["readable"]:
        state = "HOLD_UNKNOWN"
    elif observation["integrity"] != "ok":
        state = "HOLD_INVALID"
    else:
        receipts, effects = observation["receipts"], observation["effects"]
        expected = {"op_id": "op-001", "payload_hash": payload_hash(amount), "amount": amount}
        if not receipts and not effects:
            state = "REPLAY_ALLOWED"
        elif (receipts == [expected] and len(effects) == 1 and
              {k: effects[0][k] for k in expected} == expected):
            state = "COMPLETED"
        else:
            state = "HOLD_INVALID"
    lab.recovery_state = state
    lab.record("recovery", state=state, basis_snapshot_seq=lab.trace[-1]["seq"],
               scope="owned SQL-only sample; all spawned workers enumerated")
    return state


def replay(lab, name, **options):
    state = resolve(lab)
    if state not in ("REPLAY_ALLOWED", "COMPLETED"):
        lab.record("replay_blocked", child=name, state=state)
        return False
    lab.record("replay_authorized", child=name, state=state, op_id="op-001")
    lab.spawn(name, **options).finish()
    return True


def diagnose(lab, observation):
    """Return findings, never convert unreadable/invalid evidence into success."""
    findings = []
    if not observation["readable"]:
        return [{"code": "UNRESOLVED", "basis": "independent snapshot failed"}]
    receipts, effects = observation["receipts"], observation["effects"]
    counts = Counter(r["op_id"] for r in effects)
    if any(n > 1 for n in counts.values()):
        findings.append({"code": "DUPLICATE_EFFECT", "basis": dict(counts)})
    for row in receipts:
        matching = [e for e in effects if all(e[k] == row[k] for k in row)]
        if len(matching) != 1:
            findings.append({"code": "RECEIPT_EFFECT_MISMATCH", "basis": row})
    for row in effects:
        if not any(all(row[k] == r[k] for k in r) for r in receipts):
            findings.append({"code": "ORPHAN_EFFECT", "basis": row})
    claimed = any(e["event"] == "result" and e.get("status") == "COMPLETED"
                  for c in lab.children.values() for e in c.events)
    if claimed and not (len(receipts) == 1 and len(effects) == 1):
        findings.append({"code": "FALSE_COMPLETION", "basis": {
            "receipt_count": len(receipts), "effect_count": len(effects)}})
    latest_state = "UNOBSERVED"
    for e in lab.trace:
        if e["event"] == "recovery":
            latest_state = e["state"]
        elif e["event"] == "spawn" and latest_state.startswith("HOLD_"):
            # Infer from actual launch after an unresolved observation, not from
            # the negative-control's optional "unsafe_replay" annotation.
            findings.append({"code": "UNSAFE_REPLAY", "basis_seq": e["seq"],
                             "prior_recovery_state": latest_state})
    if observation["integrity"] != "ok":
        findings.append({"code": "INTEGRITY_FAILURE", "basis": observation["integrity"]})
    return findings


def facts(lab, observation, findings):
    events = [e for c in lab.children.values() for e in c.events]
    return {
        "effect_count": len(observation.get("effects", [])),
        "receipt_count": len(observation.get("receipts", [])),
        "integrity": observation.get("integrity"),
        "findings": sorted({f["code"] for f in findings}),
        "recoveries": [e["state"] for e in lab.trace if e["event"] == "recovery"],
        "blocked_replays": sum(e["event"] == "replay_blocked" for e in lab.trace),
        "kills": [e["last_checkpoint"] for e in lab.trace if e["event"] == "kill_requested"],
        "busy_phases": sorted({e["phase"] for e in events if e["event"] == "sql_error"
                               and e.get("code") == 5}),
        "busy_transaction_active": any(e["event"] == "sql_error" and e.get("phase") == "COMMIT"
                                       and e.get("in_transaction") for e in events),
        "same_transaction_retry": any(e["event"] == "commit_retried" and e.get("same_connection")
                                      and e.get("business_executions") == 1 for e in events),
        "payload_conflicts": sum(e["event"] == "result" and e.get("status") == "PAYLOAD_CONFLICT"
                                 for e in events),
        "read_failures": sum(e["event"] == "snapshot" and not e["data"]["readable"]
                             for e in lab.trace),
    }


def execute(lab, plan):
    for action in plan["actions"]:
        kind, *args = action
        if kind == "spawn":
            lab.spawn(args[0], **(args[1] if len(args) > 1 else {}))
        elif kind == "wait":
            lab.children[args[0]].wait_point(args[1])
        elif kind in ("go", "finish", "kill"):
            getattr(lab.children[args[0]], kind)()
        elif kind == "lock":
            lab.lock(args[0])
        elif kind == "unlock":
            lab.unlock()
        elif kind == "resolve":
            resolve(lab)
        elif kind == "replay":
            replay(lab, args[0], **(args[1] if len(args) > 1 else {}))
        elif kind == "unsafe_replay":
            lab.record("unsafe_replay", state=lab.recovery_state,
                       reason="intentional negative-control bypass of the replay guard")
            lab.spawn(args[0], mutant="duplicate").finish()
        else:
            raise ValueError(f"unknown action {kind}")
    observation = lab.observe()
    findings = diagnose(lab, observation)
    actual = facts(lab, observation, findings)
    checks = {}
    for key, expected in plan["expect"].items():
        checks[key] = {"expected": expected, "actual": actual[key], "pass": actual[key] == expected}
    for key, expected in plan.get("contains", {}).items():
        checks[key + "_contains"] = {"expected": expected, "actual": actual[key],
                                     "pass": all(v in actual[key] for v in expected)}
    return {"passed": all(c["pass"] for c in checks.values()), "checks": checks,
            "facts": actual, "findings": findings, "final_snapshot": observation}
