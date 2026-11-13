"""Fully automated OSS-only control using unittest + sqlite3 + subprocess.

Uses the same neutral transport and sample adapter, NEVER imports LCC engine or
its plans/checks. Scenario control and pass/fail criteria are independent Python.
"""
from __future__ import annotations
import unittest
from adapter import payload_hash


def resolve(lab):
    s = lab.observe()
    if any(c.p.poll() is None for c in lab.children.values()):
        state = "HOLD_IN_FLIGHT"
    elif not s["readable"]:
        state = "HOLD_UNKNOWN"
    elif s["integrity"] != "ok":
        state = "HOLD_INVALID"
    elif s["receipts"] == [] and s["effects"] == []:
        state = "REPLAY_ALLOWED"
    elif (s["receipts"] == [{"op_id": "op-001", "payload_hash": payload_hash(7), "amount": 7}]
          and len(s["effects"]) == 1 and s["effects"][0]["op_id"] == "op-001"
          and s["effects"][0]["amount"] == 7 and s["effects"][0]["payload_hash"] == payload_hash(7)):
        state = "COMPLETED"
    else:
        state = "HOLD_INVALID"
    lab.recovery_state = state
    lab.record("recovery", state=state, basis_snapshot_seq=lab.trace[-1]["seq"], scope="OSS sample")
    return state


def replay(lab, name):
    state = resolve(lab)
    if state not in ("REPLAY_ALLOWED", "COMPLETED"):
        lab.record("replay_blocked", child=name, state=state)
        return False
    lab.record("replay_authorized", child=name, state=state, op_id="op-001")
    lab.spawn(name).finish()
    return True


class Scenarios(unittest.TestCase):
    def __init__(self, case, lab):
        super().__init__("test_" + case)
        self.lab = lab
        self.findings = []
        self.final_snapshot = None

    def committed(self):
        s = self.lab.observe()
        self.final_snapshot = s
        self.assertTrue(s["readable"])
        self.assertEqual(s["integrity"], "ok")
        self.assertEqual(len(s["receipts"]), 1)
        self.assertEqual(len(s["effects"]), 1)
        for row in (s["receipts"][0], s["effects"][0]):
            self.assertEqual(row["op_id"], "op-001")
            self.assertEqual(row["amount"], 7)
            self.assertEqual(row["payload_hash"], payload_hash(7))

    def crash(self, point):
        a = self.lab.spawn("a", pause=point)
        a.wait_point(point)
        a.kill()
        self.assertNotEqual(a.p.returncode, 0)
        self.assertFalse(any(e["event"] == "result" for e in a.events))

    def test_T01(self):
        self.crash("before_commit")
        self.assertEqual(resolve(self.lab), "REPLAY_ALLOWED")
        self.assertTrue(replay(self.lab, "retry"))
        self.assertEqual(resolve(self.lab), "COMPLETED")
        self.committed()

    def test_T02(self):
        self.crash("after_commit")
        self.assertEqual(resolve(self.lab), "COMPLETED")
        self.assertTrue(replay(self.lab, "retry"))
        self.assertTrue(any(e.get("replayed") for e in self.lab.children["retry"].events))
        self.committed()

    def test_T03(self):
        a = self.lab.spawn("a", pause="before_commit")
        a.wait_point("before_commit")
        b = self.lab.spawn("b")
        b.finish()
        self.assertTrue(any(e.get("phase") == "BEGIN" and e.get("code") == 5 for e in b.events))
        a.go(); a.finish()
        self.assertTrue(replay(self.lab, "b_retry"))
        self.assertTrue(replay(self.lab, "sequential"))
        conflict = self.lab.spawn("conflict", amount=8)
        conflict.finish()
        self.assertTrue(any(e.get("status") == "PAYLOAD_CONFLICT" for e in conflict.events))
        self.committed()

    def test_T04(self):
        self.crash("after_commit")
        self.lab.lock("exclusive")
        self.assertEqual(resolve(self.lab), "HOLD_UNKNOWN")
        self.assertFalse(replay(self.lab, "blocked"))
        self.assertNotIn("blocked", self.lab.children)
        self.lab.unlock()
        self.assertEqual(resolve(self.lab), "COMPLETED")
        self.committed()

    def test_T05_begin(self):
        self.lab.lock("writer")
        a = self.lab.spawn("a"); a.finish()
        self.assertTrue(any(e.get("phase") == "BEGIN" and e.get("code") == 5 for e in a.events))
        self.lab.unlock()
        self.assertEqual(resolve(self.lab), "REPLAY_ALLOWED")
        self.assertTrue(replay(self.lab, "retry"))
        self.committed()

    def test_T05_commit(self):
        a = self.lab.spawn("a", pause="before_commit,commit_busy")
        a.wait_point("before_commit")
        self.lab.lock("reader")
        a.go(); a.wait_point("commit_busy")
        self.assertTrue(any(e.get("phase") == "COMMIT" and e.get("code") == 5
                            and e.get("in_transaction") for e in a.events))
        self.assertEqual(resolve(self.lab), "HOLD_IN_FLIGHT")
        self.assertFalse(replay(self.lab, "blocked"))
        self.lab.unlock()
        a.go(); a.finish()
        self.assertEqual(sum(e["event"] == "effect_executed" for e in a.events), 1)
        self.assertTrue(any(e["event"] == "commit_retried" for e in a.events))
        self.committed()

    def test_T06_duplicate(self):
        self.lab.spawn("a", mutant="duplicate").finish()
        self.lab.spawn("b", mutant="duplicate").finish()
        s = self.lab.observe(); self.final_snapshot = s
        self.assertEqual(len(s["effects"]), 2)
        self.assertEqual(len(s["receipts"]), 1)
        self.findings = ["DUPLICATE_EFFECT", "RECEIPT_EFFECT_MISMATCH", "FALSE_COMPLETION"]
        self.assertEqual(resolve(self.lab), "HOLD_INVALID")

    def test_T06_false(self):
        a = self.lab.spawn("a", mutant="false_complete"); a.finish()
        self.assertTrue(any(e.get("status") == "COMPLETED" for e in a.events))
        s = self.lab.observe(); self.final_snapshot = s
        self.assertEqual(s["effects"], [])
        self.assertEqual(s["receipts"], [])
        self.findings = ["FALSE_COMPLETION"]

    def test_T06_unsafe(self):
        self.crash("after_commit")
        self.lab.lock("exclusive")
        self.assertEqual(resolve(self.lab), "HOLD_UNKNOWN")
        self.lab.unlock()
        self.lab.record("unsafe_replay", state=self.lab.recovery_state,
                        reason="intentional negative-control bypass of the replay guard")
        self.lab.spawn("b", mutant="duplicate").finish()
        s = self.lab.observe(); self.final_snapshot = s
        self.assertEqual(len(s["effects"]), 2)
        self.assertEqual(len(s["receipts"]), 1)
        self.findings = ["DUPLICATE_EFFECT", "RECEIPT_EFFECT_MISMATCH", "FALSE_COMPLETION", "UNSAFE_REPLAY"]


CASES = ["T01", "T02", "T03", "T04", "T05_begin", "T05_commit",
         "T06_duplicate", "T06_false", "T06_unsafe"]


def execute(lab, case):
    test = Scenarios(case, lab)
    result = unittest.TestResult()
    test.run(result)
    return {"passed": result.wasSuccessful(), "unittest_tests_run": result.testsRun,
            "failures": [s for _, s in result.failures + result.errors],
            "findings": [{"code": c} for c in test.findings],
            "final_snapshot": test.final_snapshot}
