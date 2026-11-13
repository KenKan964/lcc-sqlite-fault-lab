#!/usr/bin/env python3
"""Public self-contained crash-after-COMMIT demo; synthetic SQLite data only.

Creates NEW sandbox databases under a NEW output directory. Stops only worker
processes spawned by this program. No external database or arbitrary PID API.
"""
from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import engine
from adapter import payload_hash
from lab import Lab

CASES = ("after_commit", "duplicate", "false_complete", "unsafe_replay")
SCHEMA = "demo-report-v1"


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def check(test: bool, message: str) -> None:
    if not test:
        raise AssertionError(message)


def findings_for(lab: Lab) -> tuple[dict, list[dict]]:
    observed = lab.observe()  # Different read-only connection from the writer.
    return observed, engine.diagnose(lab, observed)


def safe_after_commit(lab: Lab) -> dict:
    """The exact positive demonstration; requires BOTH DB and event evidence."""
    operation_id, amount = "op-001", 7
    writer = lab.spawn("initial", pause="after_commit", op=operation_id, amount=amount)
    checkpoint = writer.wait_point("after_commit")
    check(checkpoint.get("in_transaction") is False,
          "after_commit must be observed after the SQLite transaction ended")
    lab.record("demo_assertion", name="commit_acknowledged_in_worker_before_kill")
    writer.kill()  # The child never emits a completion response.
    check(writer.p.returncode != 0, "child was not terminated")
    check(not any(e["event"] == "result" for e in writer.events),
          "the initial application response must be lost")
    first_observation, _ = findings_for(lab)
    check(first_observation["readable"] and len(first_observation["effects"]) == 1,
          "independent readback failed to observe the committed effect")

    # This is the recovery decision, not a blind new business operation.
    recovered = engine.resolve(lab)
    check(recovered == "COMPLETED", "the recovered state must be COMPLETED")
    allowed = engine.replay(lab, "retry", op=operation_id, amount=amount)
    check(allowed, "same-operation-id resubmission was rejected")
    retry = lab.children["retry"]
    retry_results = [e for e in retry.events if e["event"] == "result"]
    check(len(retry_results) == 1 and retry_results[0].get("status") == "COMPLETED"
          and retry_results[0].get("replayed") is True,
          "second process did not return the existing committed result")
    check(not any(e["event"] == "effect_executed" for e in retry.events),
          "second process created a new business effect")
    check(retry.p.returncode == 0, "retry process failed")
    check(writer.p.pid != retry.p.pid, "retry was not observed as a distinct process")

    final_state = engine.resolve(lab)
    observed, findings = findings_for(lab)
    expected = {"op_id": operation_id, "amount": amount, "payload_hash": payload_hash(amount)}
    check(final_state == "COMPLETED", "final state was not confirmed")
    check(observed.get("integrity") == "ok", "SQLite quick_check not ok")
    check(observed["receipts"] == [expected], "incorrect or duplicate receipts")
    check(len(observed["effects"]) == 1 and all(observed["effects"][0][k] == v
                                                  for k, v in expected.items()),
          "incorrect or duplicate effects")
    check(findings == [], "unacceptable diagnostic findings")

    return {
        "scenario": "after_commit", "expected_result": "SAFE_IDEMPOTENT_REPLAY",
        "target_is_known_bad": False, "expected_finding_codes": [],
        "observed_finding_codes": [], "initial_worker_pid": writer.p.pid,
        "retry_worker_pid": retry.p.pid, "initial_returncode": writer.p.returncode,
        "initial_response_lost": True,
        "commit_checkpoint": {"name": checkpoint["point"], "in_transaction": False},
        "resubmitted_op_id": operation_id, "retry_reported_replayed": True,
        "retry_new_effect_events": 0, "state_after_restart": recovered,
        "final_state": final_state, "final_snapshot": observed,
        "passed": True,
    }


def known_bad_duplicate(lab: Lab) -> dict:
    lab.spawn("broken_initial", mutant="duplicate").finish()
    lab.spawn("broken_retry", mutant="duplicate").finish()
    state = engine.resolve(lab)
    observed, findings = findings_for(lab)
    codes = sorted({f["code"] for f in findings})
    required = {"DUPLICATE_EFFECT", "RECEIPT_EFFECT_MISMATCH"}
    check(state == "HOLD_INVALID", "duplicate target incorrectly marked safe")
    check(required.issubset(codes), "known-bad duplicate was not detected")
    check(len(observed["effects"]) == 2 and len(observed["receipts"]) == 1,
          "synthetic duplicate target did not produce two effects")
    return {"scenario": "duplicate", "expected_result": "KNOWN_BAD_DETECTED",
            "target_is_known_bad": True, "expected_finding_codes": sorted(required),
            "observed_finding_codes": codes, "final_state": state,
            "final_snapshot": observed, "passed": True}


def known_bad_false_complete(lab: Lab) -> dict:
    child = lab.spawn("broken_claim", mutant="false_complete")
    child.finish()
    check(any(e["event"] == "result" and e.get("status") == "COMPLETED" for e in child.events),
          "false-completion mutant did not claim success")
    observed, findings = findings_for(lab)
    codes = sorted({f["code"] for f in findings})
    check("FALSE_COMPLETION" in codes, "false completion was not detected")
    check(observed["effects"] == [] and observed["receipts"] == [],
          "false completion mutant unexpectedly wrote to DB")
    return {"scenario": "false_complete", "expected_result": "KNOWN_BAD_DETECTED",
            "target_is_known_bad": True, "expected_finding_codes": ["FALSE_COMPLETION"],
            "observed_finding_codes": codes, "final_snapshot": observed, "passed": True}


def known_bad_unsafe_replay(lab: Lab) -> dict:
    original = lab.spawn("initial", pause="after_commit")
    original.wait_point("after_commit")
    original.kill()
    lab.lock("exclusive")
    try:
        state = engine.resolve(lab)
        check(state == "HOLD_UNKNOWN", "readback lock did not create unknown state")
        check(engine.replay(lab, "blocked") is False, "safety guard allowed unknown-state replay")
    finally:
        lab.unlock()
    # Intentional negative control: bypass guard to test the diagnostic oracle.
    lab.spawn("unsafe", mutant="duplicate").finish()
    observed, findings = findings_for(lab)
    codes = sorted({f["code"] for f in findings})
    required = {"UNSAFE_REPLAY", "DUPLICATE_EFFECT"}
    check(required.issubset(codes), "unsafe replay was not detected")
    check(len(observed["effects"]) == 2, "unsafe test did not create duplicate")
    return {"scenario": "unsafe_replay", "expected_result": "KNOWN_BAD_DETECTED",
            "target_is_known_bad": True, "expected_finding_codes": sorted(required),
            "observed_finding_codes": codes, "last_guarded_state": state,
            "final_snapshot": observed, "passed": True}


SCENARIOS = {
    "after_commit": safe_after_commit,
    "duplicate": known_bad_duplicate,
    "false_complete": known_bad_false_complete,
    "unsafe_replay": known_bad_unsafe_replay,
}


def run_demo(out: Path, selected: list[str]) -> dict:
    """Never reuse or delete an output directory. No external path to a DB."""
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    results: list[dict] = []
    for name in selected:
        case_dir = out / name
        case_dir.mkdir(exist_ok=False)
        lab = Lab(case_dir)
        record: dict
        try:
            record = SCENARIOS[name](lab)
        except Exception as exc:
            # The failure is recorded; a demo exception is NOT a successful defect detection.
            record = {"scenario": name, "passed": False, "error_type": type(exc).__name__,
                      "error_message": str(exc), "target_is_known_bad": name != "after_commit"}
        finally:
            lab.cleanup()
        record["evidence"] = {"trace": f"{name}/trace.jsonl",
                              "database": f"{name}/lab.sqlite",
                              "result": f"{name}/result.json"}
        record["child_events"] = lab.archive()["children"]
        write_json(case_dir / "result.json", record)
        results.append(record)

    summary = {
        "report_schema": SCHEMA,
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(), "python_version": platform.python_version(),
        "sqlite_version": sqlite3.sqlite_version,
        "source": "DEV-001 Python/SQLite bounded synthetic laboratory",
        "requested": selected, "total": len(results),
        "passed": sum(x["passed"] for x in results),
        "all_expectations_met": all(x["passed"] for x in results),
        "interpretation": (
            "A passed known-bad case means the tool DETECTED an intentionally injected defect; "
            "it does not mean the defective application is safe."
        ),
        "scope_limitations": ["Synthetic single SQLite DB; controlled child processes",
                              "No real power loss, fsync fault, WAL crash, external effects",
                              "Not an actual Drift or Room integration",
                              "No protection against unknown external writers"],
        "cases": [{"scenario": x["scenario"], "passed": x["passed"],
                   "target_is_known_bad": x.get("target_is_known_bad", False),
                   "finding_codes": x.get("observed_finding_codes", []),
                   "evidence": x["evidence"]} for x in results],
    }
    write_json(out / "summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True,
                        help="A NEW output directory; never points to existing data")
    parser.add_argument("--case", action="append", choices=CASES,
                        help="Run only selected case(s); omit to run all four")
    args = parser.parse_args(argv)
    selected = list(dict.fromkeys(args.case)) if args.case else list(CASES)
    try:
        summary = run_demo(args.out, selected)
    except (OSError, ValueError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"report": str(args.out.resolve() / "summary.json"),
                      "total": summary["total"], "passed": summary["passed"],
                      "all_expectations_met": summary["all_expectations_met"]},
                     ensure_ascii=False))
    return 0 if summary["all_expectations_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
