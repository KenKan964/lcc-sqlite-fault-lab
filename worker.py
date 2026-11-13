"""One-shot test-only process; line JSON protocol; cooperative barriers, real kill."""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from adapter import connect, payload_hash, validate_sandbox


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sandbox", required=True, type=Path)
    ap.add_argument("--token", required=True)
    ap.add_argument("--op", default="op-001")
    ap.add_argument("--amount", type=int, default=7)
    ap.add_argument("--pause", default="")
    ap.add_argument("--mutant", choices=("none", "duplicate", "false_complete"), default="none")
    args = ap.parse_args()
    path = validate_sandbox(args.sandbox, args.token)
    pauses = set(args.pause.split(","))

    def emit(event: str, **data):
        print(json.dumps({"event": event, "pid": os.getpid(), "op_id": args.op,
                          **data}), flush=True)

    def hook(name: str, **data):
        emit("checkpoint", point=name, **data)
        if name in pauses and sys.stdin.readline().strip() != "continue":
            raise RuntimeError("barrier continuation missing")

    c = connect(path)
    phase = "BEGIN"
    try:
        hook("before_begin")
        if args.mutant == "false_complete":
            emit("result", status="COMPLETED", replayed=False)
            return 0
        c.execute("BEGIN IMMEDIATE")
        phase = "EXECUTE"
        digest = payload_hash(args.amount)
        prior = c.execute("SELECT payload_hash,amount FROM receipts WHERE op_id=?",
                          (args.op,)).fetchone()
        if prior and prior != (digest, args.amount):
            c.execute("ROLLBACK")
            emit("result", status="PAYLOAD_CONFLICT")
            return 0
        if prior and args.mutant == "none":
            c.execute("ROLLBACK")
            emit("result", status="COMPLETED", replayed=True)
            return 0
        c.execute("INSERT INTO effects(op_id,payload_hash,amount) VALUES(?,?,?)",
                  (args.op, digest, args.amount))
        c.execute("INSERT OR IGNORE INTO receipts VALUES(?,?,?)", (args.op, digest, args.amount))
        emit("effect_executed", count=1)
        hook("before_commit", in_transaction=c.in_transaction)
        phase = "COMMIT"
        try:
            c.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            emit("sql_error", phase=phase, code=exc.sqlite_errorcode,
                 name=exc.sqlite_errorname, in_transaction=c.in_transaction)
            if exc.sqlite_errorcode != sqlite3.SQLITE_BUSY or "commit_busy" not in pauses:
                raise
            hook("commit_busy", in_transaction=c.in_transaction)
            # Retry the SAME transaction, never the business operation.
            c.execute("COMMIT")
            emit("commit_retried", same_connection=True, business_executions=1)
        hook("after_commit", in_transaction=c.in_transaction)
        emit("result", status="COMPLETED", replayed=False)
        return 0
    except sqlite3.Error as exc:
        active = c.in_transaction
        if active:
            c.execute("ROLLBACK")
        emit("sql_error", phase=phase, code=getattr(exc, "sqlite_errorcode", None),
             name=getattr(exc, "sqlite_errorname", None), in_transaction=active,
             rollback_confirmed=not c.in_transaction)
        # Known rollback is not an unknown commit response.
        emit("result", status="ROLLED_BACK")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    raise SystemExit(main())
