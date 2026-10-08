---
description: Run the test suite, diagnose failures, and summarize results against the requirements
---
Run the project tests and report. Do not change code unless I ask.

1. `. .venv/bin/activate && pytest -q` (all tests, uses moto: no AWS needed).
2. If anything fails: re-run just that test with `-x -vv`, read the failing code path, and state the
   **root cause** (not just the assertion). Say whether it is a product bug, a test bug, or a moto/limitation issue.
3. Summarize in this table (counts from the actual output, never from memory):

| Area | Test files | Result |
|---|---|---|
| State model | test_models.py | |
| Basic run / durability | test_engine_basic.py | |
| Human-in-the-loop & duplicate answers | test_engine_hitl.py | |
| Stop / resume | test_engine_stop_resume.py | |
| Failure & retry | test_failure_retry.py | |
| Idempotency & concurrency | test_idempotency.py, test_store_guards.py | |
| Handlers / SFN adapter | test_handlers_and_orchestrator.py | |
| Infra synth + state machine glue | test_infra.py, test_state_machine_integration.py | |

4. Finish with the exact total (`N passed, M failed`) and anything skipped (e.g. node missing => infra tests skipped).
Never claim a pass you did not see.
