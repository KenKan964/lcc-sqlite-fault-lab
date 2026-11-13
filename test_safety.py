"""Safety contract and detector negative controls, separate from scenario claims."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import engine
from adapter import connect, payload_hash, validate_sandbox
from lab import Lab

HERE = Path(__file__).resolve().parent


class Contracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dev001_contract_")
        self.directory = Path(self.tmp.name)
        self.lab = Lab(self.directory)

    def tearDown(self):
        self.lab.cleanup()
        self.tmp.cleanup()

    def test_wrong_sandbox_token_refused(self):
        with self.assertRaises(ValueError):
            validate_sandbox(self.directory, "wrong-token")

    def test_external_database_symlink_refused(self):
        link_dir = self.directory / "malicious"
        link_dir.mkdir()
        (link_dir / "SANDBOX.json").write_text(json.dumps({"token": self.lab.token}))
        (link_dir / "lab.sqlite").symlink_to(self.lab.path)
        with self.assertRaises(ValueError):
            validate_sandbox(link_dir, self.lab.token)

    def test_read_failure_cannot_be_completed_or_replayed(self):
        self.lab.spawn("first").finish()
        self.lab.lock("exclusive")
        self.assertEqual(engine.resolve(self.lab), "HOLD_UNKNOWN")
        self.assertFalse(engine.replay(self.lab, "blocked"))
        self.assertNotIn("blocked", self.lab.children)
        self.lab.unlock()
        self.assertEqual(engine.resolve(self.lab), "COMPLETED")

    def test_absence_during_live_uncommitted_worker_cannot_authorize_retry(self):
        c = self.lab.spawn("a", pause="before_commit")
        c.wait_point("before_commit")
        self.assertEqual(engine.resolve(self.lab), "HOLD_IN_FLIGHT")
        self.assertFalse(engine.replay(self.lab, "blocked"))
        c.kill()
        self.assertEqual(engine.resolve(self.lab), "REPLAY_ALLOWED")

    def test_orphan_effect_holds(self):
        c = connect(self.lab.path)
        c.execute("INSERT INTO effects(op_id,payload_hash,amount) VALUES(?,?,?)",
                  ("op-001", payload_hash(7), 7))
        c.close()
        self.assertEqual(engine.resolve(self.lab), "HOLD_INVALID")
        findings = engine.diagnose(self.lab, self.lab.observe())
        self.assertIn("ORPHAN_EFFECT", [f["code"] for f in findings])

    def test_payload_mismatch_holds(self):
        self.lab.spawn("a").finish()
        c = connect(self.lab.path)
        c.execute("UPDATE effects SET amount=8")
        c.close()
        self.assertEqual(engine.resolve(self.lab), "HOLD_INVALID")
        self.assertFalse(engine.replay(self.lab, "blocked"))

    def test_unannotated_unsafe_launch_is_detected(self):
        self.lab.spawn("a").finish()
        self.lab.lock("exclusive")
        self.assertEqual(engine.resolve(self.lab), "HOLD_UNKNOWN")
        self.lab.unlock()
        # Deliberately no unsafe_replay annotation.
        self.lab.spawn("b", mutant="duplicate").finish()
        findings = engine.diagnose(self.lab, self.lab.observe())
        codes = [f["code"] for f in findings]
        self.assertIn("UNSAFE_REPLAY", codes)
        self.assertIn("DUPLICATE_EFFECT", codes)

    def test_correct_replay_has_no_findings(self):
        self.lab.spawn("a").finish()
        self.assertTrue(engine.replay(self.lab, "b"))
        self.assertEqual(engine.diagnose(self.lab, self.lab.observe()), [])

    def test_no_fault_mutant_fails_duplicate_expectation(self):
        plans = json.loads((HERE / "plans.json").read_text())
        p = plans["T06_duplicate"]
        for action in p["actions"]:
            if action[0] == "spawn":
                action[2] = {"mutant": "none"}
        self.assertFalse(engine.execute(self.lab, p)["passed"])

    def test_disabled_detector_fails_negative_control(self):
        plan = json.loads((HERE / "plans.json").read_text())["T06_false"]
        with patch("engine.diagnose", return_value=[]):
            result = engine.execute(self.lab, plan)
        self.assertFalse(result["passed"])

    def test_worker_failure_does_not_look_completed(self):
        c = self.lab.spawn("a", pause="before_commit")
        c.wait_point("before_commit")
        c.kill()
        self.assertNotEqual(c.p.returncode, 0)
        self.assertEqual(engine.resolve(self.lab), "REPLAY_ALLOWED")
        self.assertEqual(self.lab.observe()["effects"], [])


class CLIContracts(unittest.TestCase):
    def test_refuses_existing_output_and_preserves_file(self):
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "preserve.txt"
            marker.write_text("keep")
            r = subprocess.run([sys.executable, str(HERE / "cli.py"), "run", "--out", td],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 2)
            self.assertEqual(marker.read_text(), "keep")
            self.assertEqual(sorted(p.name for p in Path(td).iterdir()), ["preserve.txt"])

    def test_nonpositive_repeat_refused(self):
        r = subprocess.run([sys.executable, str(HERE / "cli.py"), "run", "--repeat", "0",
                            "--out", "should_not_exist"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
