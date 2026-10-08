---
description: Mutation-check the idempotency/atomicity guards (each removed guard must make a test fail)
---
Run `. .venv/bin/activate && python scripts/guard_mutation_check.py`.

- It removes ONE guard at a time (transaction conditions, ASL retry/catch/payload wiring), runs the
  relevant tests, and restores the file.
- Every mutation must be **caught**. A line starting with `SURVIVED` means that guard is not independently
  tested: add a test to `tests/test_store_guards.py` (rig state so only that guard can reject) or to
  `tests/test_state_machine_integration.py`, then re-run.
- If you added a new guard, add a matching entry to `MUTATIONS` in the script first (it should survive
  until your test exists - that is the point).
- Confirm `git status` shows no modified source files afterwards (the script restores them).
Report the `N/M mutations caught` line verbatim.
