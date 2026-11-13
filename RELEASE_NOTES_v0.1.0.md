# v0.1.0 — public GitHub demo candidate

**Purpose:** A safe, reproducible demonstration of COMMIT-after-crash recovery and exactly-once *local, synthetic* business effects by means of idempotent operation IDs.

## Included

- `demo.py`: public four-case demo (`after_commit`, `duplicate`, `false_complete`, `unsafe_replay`).
- Separate test child process killed at a named `after_commit` checkpoint, after confirmed SQLite COMMIT but before application acknowledgement.
- Independent readback, new child process with same operation ID, and dual proof of no second business effect (DB state plus child event).
- JSON summary/results and JSONL ordered traces, automatic negative-control defect detection.
- OSS-only DEV-001 comparator, self-contained tests, GitHub Actions on Linux and docs in English/Japanese.
- Worker argument allowlist to prevent arbitrary database paths/tokens from being overridden by plans.

## Verified in this preparation

- Local development environment: Linux, Python 3.13.5, SQLite 3.46.1.
- Demo test suite: 4/4 pass.
- Original safety contract suite: 13/13 pass.
- DEV-001 OSS-vs-LCC test case T02: 2/2 pass, observations match.

## Not claimed

No production or customer DB integration; no real power loss or arbitrary crash point in SQLite internals; no guarantee covering WAL, fsync, external effects, or unknown writers. No Drift/Room runtime test, Windows/macOS verification, external user adoption, human labor measurements, or proven commercial advantage.

**Publication is not performed by this artifact.** The publisher must confirm code ownership, choose the license, review source and run CI before making a public repository.
