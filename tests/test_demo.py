"""Independent public-demo acceptance tests, stdlib-only."""
from __future__ import annotations
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]


class PublicDemo(unittest.TestCase):
    def test_complete_demo_and_sqlite_evidence(self):
        with tempfile.TemporaryDirectory(prefix="lcc_public_demo_") as td:
            output = Path(td) / "results"
            proc = subprocess.run([sys.executable, str(BASE / "demo.py"), "--out", str(output)],
                                  text=True, capture_output=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["total"], 4)
            self.assertEqual(summary["passed"], 4)
            self.assertTrue(summary["all_expectations_met"])
            results = {name: json.loads((output / name / "result.json").read_text())
                       for name in ("after_commit", "duplicate", "false_complete", "unsafe_replay")}
            safe = results["after_commit"]
            self.assertEqual(safe["state_after_restart"], "COMPLETED")
            self.assertTrue(safe["initial_response_lost"])
            self.assertTrue(safe["retry_reported_replayed"])
            self.assertEqual(safe["retry_new_effect_events"], 0)
            self.assertEqual(safe["initial_returncode"] != 0, True)
            self.assertNotEqual(safe["initial_worker_pid"], safe["retry_worker_pid"])
            with sqlite3.connect(output / "after_commit" / "lab.sqlite") as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM effects").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 1)
                self.assertEqual(con.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertIn("DUPLICATE_EFFECT", results["duplicate"]["observed_finding_codes"])
            self.assertIn("FALSE_COMPLETION", results["false_complete"]["observed_finding_codes"])
            self.assertIn("UNSAFE_REPLAY", results["unsafe_replay"]["observed_finding_codes"])
            self.assertEqual(sum(1 for line in (output / "after_commit" / "trace.jsonl").read_text().splitlines()
                                 if json.loads(line)["event"] == "kill_requested"), 1)

    def test_plan_cannot_override_test_db_path_or_token(self):
        from contextlib import suppress
        sys.path.insert(0, str(BASE))
        try:
            from lab import Lab
            with tempfile.TemporaryDirectory(prefix="lcc_secure_args_") as td:
                root = Path(td) / "sandbox"
                root.mkdir()
                lab = Lab(root)
                try:
                    with self.assertRaises(ValueError):
                        lab.spawn("hostile", sandbox="/tmp/outside.sqlite")
                    with self.assertRaises(ValueError):
                        lab.spawn("hostile2", token="overwritten")
                    with self.assertRaises(ValueError):
                        lab.spawn("hostile3", pause="invalid_hook")
                    self.assertEqual(lab.children, {})
                finally:
                    lab.cleanup()
        finally:
            with suppress(ValueError):
                sys.path.remove(str(BASE))

    def test_existing_destination_refused_without_modification(self):
        with tempfile.TemporaryDirectory(prefix="lcc_public_refusal_") as td:
            root = Path(td)
            marker = root / "existing.txt"
            marker.write_text("not touched", encoding="utf-8")
            proc = subprocess.run([sys.executable, str(BASE / "demo.py"), "--out", str(root)],
                                  text=True, capture_output=True, timeout=30)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(marker.read_text(encoding="utf-8"), "not touched")
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["existing.txt"])

    def test_single_case_select(self):
        with tempfile.TemporaryDirectory(prefix="lcc_public_single_") as td:
            out = Path(td) / "r"
            proc = subprocess.run([sys.executable, str(BASE / "demo.py"), "--out", str(out),
                                   "--case", "after_commit"], text=True, capture_output=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            summary = json.loads((out / "summary.json").read_text())
            self.assertEqual(summary["total"], 1)
            self.assertTrue((out / "after_commit" / "result.json").is_file())
            self.assertFalse((out / "duplicate").exists())


if __name__ == "__main__":
    unittest.main()
