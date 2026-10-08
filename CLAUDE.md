# CLAUDE.md

Durable, pausable **agent runner**: a multi-step run where an "agent" step may ask a human
a question. Runs can wait for days with zero compute, be stopped, and be resumed (also after
failure) without redoing completed steps or repeating side effects.

The business workload is deliberately trivial (validate → agent decision → [human approval] →
finalize). **The runner is the product.** Do not grow the workload.

## Stack
Python 3.12 · AWS Step Functions (Standard) · Lambda · DynamoDB (single table) · AWS CDK (Python)
· pytest + moto. Lambda code has **zero third-party deps** (boto3 ships in the runtime).
Do not add dependencies without a written reason in docs/DESIGN.md.

## Layout
- `src/agent_runner/models.py`    run/step states + the transition table (single source of truth)
- `src/agent_runner/agent.py`     `Agent` interface + `StubAgent`. No orchestration logic here.
- `src/agent_runner/pipeline.py`  pure step functions (no I/O except via the returned outcome)
- `src/agent_runner/store.py`     ALL DynamoDB access. Conditional writes + transactions live here.
- `src/agent_runner/engine.py`    control ops (start/stop/resume/answer/status) + worker ops (route/run_step/...)
- `src/agent_runner/orchestrator.py`  `Orchestrator` port: `StepFunctionsOrchestrator`, `LocalOrchestrator`
- `src/agent_runner/handlers.py`  Lambda entrypoints (`control`, `worker`)
- `src/agent_runner/cli.py`       `runner` CLI (aws mode = invokes control Lambda; local mode = in-process)
- `infra/`                        CDK app. `tests/` mirrors the requirements.

## Invariants (never break these)
1. **DynamoDB is the source of truth.** Step Functions only *executes*. Never derive run state from
   an execution's status or from in-memory data.
2. **Every state change goes through the transition table** in `models.py` and is a *conditional* write
   (`status IN (allowed sources)`). No read-then-write without a condition.
3. **A step's result, the pointer advance (`current_step`), the status change and any side effect are
   committed in ONE DynamoDB transaction.** Either all happen or none.
4. **Side effects are recorded as `EFFECT#<step>` items with `attribute_not_exists`.** A duplicate
   cannot create a second one. Real external calls must use `run_id+step_id` as idempotency key.
5. **A step already `DONE` is never re-executed**; its stored result is returned (retry = reuse).
6. **Human requests are identified by `request_id`**, never by question text. Answering is a
   PENDING→ANSWERED conditional transition; a repeat of the same answer is a no-op success, a
   different answer is a conflict.
7. **Waiting never holds compute.** The wait is `waitForTaskToken`; the token is stored on the REQ item.
8. **Start and resume share one code path:** a new execution begins at `route`, which reads DynamoDB.
9. Lambda error *class names* are matched by Step Functions Retry. `TransientError` is retried,
   everything else is permanent. Never subclass `TransientError`.

## Verification tools (use them, they have caught real bugs)
- `tests/test_store_guards.py`: every transaction guard is tested **in isolation** (rig state so only that guard rejects).
- `python scripts/guard_mutation_check.py` (`/guard-check`): removes 13 guards/ASL wirings one at a time; each must make a test fail.
  If you add a guard, add a mutation for it.
- `tests/asl_sim.py` + `test_state_machine_integration.py`: execute the *synthesized* state machine against the real handlers.
  It approximates Step Functions; it does not replace a real deploy.
- moto transactions are NOT thread-safe; `tests/conftest.py` serializes raw DynamoDB calls to emulate DynamoDB. Don't "fix" flaky
  concurrency tests by removing that lock.

## Commands
```
make install        # venv + deps
make test           # pytest (moto, no AWS needed)
make demo-local     # full scripted demo against a local moto server
make synth          # cdk synth
make deploy         # cdk deploy  (needs AWS creds in env/profile)
make destroy        # teardown
```
Single test: `. .venv/bin/activate && pytest tests/test_engine_hitl.py -k duplicate -x`

## Conventions
- Small functions, type hints, no clever metaprogramming. Errors are explicit classes in `errors.py`.
- Any new store write that changes run state needs: a transition-table entry, a test for the
  invalid-source case, and a test for the duplicate/concurrent case.
- Tests assert behaviour through the public engine API, not private store internals.
- Update docs/DESIGN.md when a semantic changes. Keep this file current.

## Security / guardrails
- Never commit credentials, tokens, `.env` files or account IDs. No hard-coded ARNs/regions.
- Local demo uses dummy creds only via environment at runtime (see `scripts/demo_local.sh`).
- IAM: grant each Lambda the minimum (table RW, `states:StartExecution/StopExecution/SendTask*` only where needed).
- Out of scope (describe, don't build): auth, UI, multi-tenancy, production observability.

## Workflow for AI agents
Use `/test-run` after every change and `/review` before declaring work done. Use the
`idempotent-step` skill when adding or modifying any step or control operation.
