#!/usr/bin/env python3
"""DEV-001 reproducible CLI. No external DB, executable or PID parameters."""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import platform
import random
import sqlite3
import statistics
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
import engine
import oss_baseline
from lab import Lab

HERE = Path(__file__).resolve().parent
CATEGORIES = ["initial_setup", "scenario_addition", "target_code_change", "reproduction",
              "result_judgment", "cause_investigation"]


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fingerprint(result):
    s = result.get("final_snapshot") or {}
    return {"passed": result["passed"], "snapshot": s,
            "findings": sorted(f["code"] for f in result.get("findings", []))}


def run(args):
    plans = json.loads((args.plans or HERE / "plans.json").read_text(encoding="utf-8"))
    cases = args.case or list(oss_baseline.CASES)
    for case in cases:
        if case not in plans:
            raise ValueError(f"missing plan: {case}")
    # Fail rather than alter an existing output directory.
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter_ns()
    metadata = {"schema_version": "1.0", "date_utc": datetime.now(timezone.utc).isoformat(),
                "python": sys.version, "sqlite": sqlite3.sqlite_version,
                "platform": platform.platform(), "journal_mode": "delete", "synchronous": "FULL",
                "busy_timeout_ms": 0, "seed": args.seed, "repeat": args.repeat,
                "cases": cases, "command": sys.argv, "human_effort": "UNMEASURED",
                "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in sorted(HERE.glob("*.py"))},
                "plan_sha256": hashlib.sha256((args.plans or HERE / "plans.json").read_bytes()).hexdigest(),
                "scope": "test child crash; not power-loss, disk I/O failure or production validation"}
    write_json(out / "environment.json", metadata)
    rows, results = [], []
    rng = random.Random(args.seed)
    for iteration in range(args.repeat):
        for case in cases:
            modes = ["oss", "lcc"] if args.mode == "both" else [args.mode]
            rng.shuffle(modes)  # seeded crossover avoids a fixed first-arm order
            for mode in modes:
                case_dir = Path(tempfile.mkdtemp(prefix=f"{iteration:03d}_{case}_{mode}_", dir=out))
                t0 = time.perf_counter_ns()
                lab = None
                try:
                    lab = Lab(case_dir)
                    result = (engine.execute(lab, plans[case]) if mode == "lcc"
                              else oss_baseline.execute(lab, case))
                except Exception:
                    result = {"passed": False, "error": traceback.format_exc()}
                finally:
                    if lab:
                        try:
                            lab.cleanup()
                        except Exception:
                            result = {**result, "passed": False, "cleanup_error": traceback.format_exc()}
                elapsed = (time.perf_counter_ns() - t0) / 1e6
                result.update({"mode": mode, "case": case, "iteration": iteration,
                               "runtime_ms": elapsed, "sandbox": case_dir.name})
                if lab:
                    result["evidence"] = lab.archive()
                write_json(case_dir / "result.json", result)
                results.append(result)
                rows.append({"iteration": iteration, "case": case, "mode": mode,
                             "passed": result["passed"], "runtime_ms": round(elapsed, 6),
                             "result_path": str((case_dir / "result.json").relative_to(out))})
    with (out / "measurements.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rows[0]); w.writeheader(); w.writerows(rows)
    comparison = []
    if args.mode == "both":
        for iteration in range(args.repeat):
            for case in cases:
                pair = {r["mode"]: r for r in results if r["case"] == case and r["iteration"] == iteration}
                comparison.append({"iteration": iteration, "case": case,
                                   "equal": fingerprint(pair["oss"]) == fingerprint(pair["lcc"])})
    aggregates = []
    for case in cases:
        for mode in ("oss", "lcc"):
            group = [r for r in results if r["case"] == case and r["mode"] == mode]
            if group:
                times = [r["runtime_ms"] for r in group]
                aggregates.append({"case": case, "mode": mode, "n": len(group),
                                   "pass": sum(r["passed"] for r in group),
                                   "median_ms": statistics.median(times),
                                   "min_ms": min(times), "max_ms": max(times)})
    summary = {"total": len(results), "passed": sum(r["passed"] for r in results),
               "arm_equivalence": comparison, "aggregates": aggregates,
               "batch_runtime_ms": (time.perf_counter_ns() - started) / 1e6,
               "human_effort": {mode: {category: None for category in CATEGORIES}
                                for mode in ("oss", "lcc")},
               "human_savings_percent": None,
               "decision": "technical research conditional GO; commercial value NO-GO"}
    write_json(out / "summary.json", summary)
    write_json(out / "plans_used.json", plans)
    ok = summary["passed"] == summary["total"] and all(c["equal"] for c in comparison)
    print(json.dumps({"output": str(out), "total": summary["total"], "passed": summary["passed"],
                      "equivalence_passed": all(c["equal"] for c in comparison), "ok": ok}))
    return 0 if ok else 1


def effort(args):
    # Explicit self-timed work intervals; never infer human activity from runtime.
    path = args.log.resolve()
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    if args.action == "start":
        if records and records[-1]["event"] == "start":
            raise ValueError("stop current interval first")
        if not args.mode or not args.category or not args.operator:
            raise ValueError("start requires mode, category and pseudonymous operator")
        record = {"event": "start", "mode": args.mode, "category": args.category,
                  "operator": args.operator, "task": args.task, "epoch_ns": time.time_ns(),
                  "monotonic_ns": time.monotonic_ns(), "clock": platform.node()}
    else:
        if not records or records[-1]["event"] != "start":
            raise ValueError("no open interval")
        start = records[-1]
        if start["clock"] != platform.node():
            raise ValueError("stop on the same host")
        record = {**start, "event": "stop", "epoch_ns": time.time_ns(),
                  "monotonic_ns": time.monotonic_ns()}
        record["self_timed_seconds"] = (record["monotonic_ns"] - start["monotonic_ns"]) / 1e9
        record["measurement_kind"] = "operator-declared active interval; not independently verified"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, ensure_ascii=False))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run")
    p.add_argument("--mode", choices=("oss", "lcc", "both"), default="both")
    p.add_argument("--case", action="append", choices=oss_baseline.CASES)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--seed", type=int, default=1001)
    p.add_argument("--plans", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("effort")
    p.add_argument("action", choices=("start", "stop"))
    p.add_argument("--log", type=Path, required=True)
    p.add_argument("--mode", choices=("oss", "lcc"))
    p.add_argument("--category", choices=CATEGORIES)
    p.add_argument("--operator")
    p.add_argument("--task", default="")
    args = ap.parse_args()
    if args.command == "run" and args.repeat < 1:
        ap.error("repeat must be >= 1")
    try:
        return run(args) if args.command == "run" else effort(args)
    except (ValueError, OSError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
