"""SQLite-only sample adapter. Never connect to an externally supplied database."""
from __future__ import annotations
import hashlib
import json
import sqlite3
from pathlib import Path


def payload_hash(amount: int) -> str:
    return hashlib.sha256(json.dumps({"amount": amount}, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                            timeout=0, isolation_level=None)
    else:
        c = sqlite3.connect(path, timeout=0, isolation_level=None)
    c.execute("PRAGMA busy_timeout=0")
    return c


def initialize(directory: Path, token: str) -> Path:
    """Only a newly created empty laboratory directory is accepted."""
    if any(directory.iterdir()):
        raise ValueError("sandbox must be empty")
    (directory / "SANDBOX.json").write_text(json.dumps({"token": token}), encoding="utf-8")
    path = directory / "lab.sqlite"
    c = connect(path)
    try:
        c.execute("PRAGMA journal_mode=DELETE")
        c.execute("PRAGMA synchronous=FULL")
        c.executescript("""
        CREATE TABLE receipts(op_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
                              amount INTEGER NOT NULL);
        CREATE TABLE effects(seq INTEGER PRIMARY KEY, op_id TEXT NOT NULL,
                             payload_hash TEXT NOT NULL, amount INTEGER NOT NULL);
        """)
        # effects has no UNIQUE constraint intentionally: independent oracle must
        # catch a broken adapter. Correct adapter serializes and deduplicates.
    finally:
        c.close()
    return path


def validate_sandbox(directory: Path, token: str) -> Path:
    if directory.is_symlink() or (directory / "lab.sqlite").is_symlink():
        raise ValueError("symlinks not accepted")
    marker = json.loads((directory / "SANDBOX.json").read_text(encoding="utf-8"))
    if marker != {"token": token} or not (directory / "lab.sqlite").is_file():
        raise ValueError("not an owned laboratory sandbox")
    return directory / "lab.sqlite"


def snapshot(path: Path) -> dict:
    c = connect(path, readonly=True)
    try:
        c.execute("BEGIN")
        receipts = [dict(zip(("op_id", "payload_hash", "amount"), r)) for r in
                    c.execute("SELECT op_id,payload_hash,amount FROM receipts ORDER BY op_id")]
        effects = [dict(zip(("seq", "op_id", "payload_hash", "amount"), r)) for r in
                   c.execute("SELECT seq,op_id,payload_hash,amount FROM effects ORDER BY seq")]
        integrity = c.execute("PRAGMA quick_check").fetchone()[0]
        c.execute("ROLLBACK")
        return {"receipts": receipts, "effects": effects, "integrity": integrity}
    finally:
        c.close()
