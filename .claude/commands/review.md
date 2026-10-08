---
description: Review the implementation against the assessment requirements (state, HITL, idempotency, stop/resume, failure, tests, security, docs)
---
Review the repo as a skeptical senior engineer. Read `README.md` (the assessment), `CLAUDE.md`, `docs/DESIGN.md`,
then the code. Do NOT assume anything works: open the code and the tests. Report findings, do not fix yet.

Check each area; for every item cite `file:line` and the test that proves it (or say "no test"):

1. **State model**: every status change goes through `models.TRANSITIONS` and a conditional write?
2. **Human-in-the-loop**: request persisted before the run pauses; wait holds no compute; request found by `request_id`.
3. **Idempotency**: duplicate answer, duplicate start, retried step, concurrent workers, lost wake-up. Any read-then-write without a condition?
4. **Stop**: from RUNNING and WAITING; pending request preserved; in-flight step result discarded.
5. **Resume**: from STOPPED and FAILED; completed steps not re-run (`invocations`); same `request_id` reused.
6. **Failure/retry**: transient vs permanent; what is retried by whom; error persisted; `FAILED` not overwriting `STOPPED`.
7. **Tests**: behaviour not implementation; is each guard tested in isolation? Run `python scripts/guard_mutation_check.py`.
8. **Security**: secrets, hard-coded ARNs/regions, IAM breadth, `.gitignore`.
9. **Docs**: does DESIGN.md still match the code? README commands actually work? AI_USAGE.md only claims what happened?

Output: a table `Requirement | Implemented | Tested | Documented` with ✅/❌, then a prioritized list of
concrete problems (blocker / should-fix / nit). Be specific and honest; "looks fine" is not a finding.
