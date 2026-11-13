# GitHub publication checklist (publisher action required)

The CLI demo and tests are locally prepared, but the repository has **not** been uploaded or made public by this work.

- [ ] Confirm copyright ownership and permission to distribute the DEV-001-derived source under MIT. If the licensing decision differs, update `LICENSE`, README, and downstream notices **before publishing**.
- [ ] Review every source file and the git history for proprietary identifiers, customer data, credentials, proprietary tests and unapproved dependencies.
- [ ] Run both Python unittest commands shown in README in a fresh environment.
- [ ] Run `python demo.py --out ./demo_run_001` in a NEW output location and inspect evidence.
- [ ] Verify `.gitignore` excludes DB files, virtual environments, evidence and run outputs.
- [ ] Create a new public GitHub repository, choose an accurate description and link to README.
- [ ] Push only the reviewed source tree (do not commit generated output).
- [ ] Confirm GitHub Actions succeeds on the public repository.
- [ ] Mark initial release as `v0.1.0` / **experimental**, not production-safe or commercial-ready.
- [ ] Handle bug reports without requesting proprietary customer datasets or production access.

Recommended description: **"Experimental Python/SQLite fault-injection CLI: crash after COMMIT, same-operation-ID replay, idempotency and diagnostic evidence."**
