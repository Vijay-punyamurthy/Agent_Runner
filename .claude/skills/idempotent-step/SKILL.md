---
name: idempotent-step
description: How to add, change or review a pipeline step or control operation in this agent runner so that retries, duplicate requests, resumes and concurrent workers can never repeat a side effect or corrupt run state. Use whenever touching pipeline.py, engine.py (run_step/answer/stop/resume) or store.py.
---

# Idempotent step / control operation

This repo's whole value is "safe to retry, safe to resume". Every change must preserve it.
Read `CLAUDE.md` invariants first. Then follow this checklist **in order**.

## A. Adding a new pipeline step

1. **Pure function in `pipeline.py`**: `fn(context, agent) -> StepOutcome(result, human=None, effect=None)`.
   No I/O, no clock, no randomness. It may be called more than once before one call "wins"; only the
   winner's result is stored. Put non-determinism (LLM calls) *inside the agent*, never in the engine.
2. **Register it**: add the id to `models.STEP_ORDER`, to `pipeline.STEP_FUNCTIONS`, and to `_next_after`
   in `engine.py` (the pointer advance). Keep ids snake_case (they are DynamoDB map keys).
3. **Side effect?** Return it as `StepOutcome.effect`. The store records it as `EFFECT#<step_id>` with
   `attribute_not_exists` *inside the same transaction* as the step result. Never call an external
   system from the pure function. If a real external call is unavoidable, use `run_id + step_id` as the
   provider's idempotency key and perform it from a step whose outcome is committed atomically with a
   "call started" marker (outbox pattern), then document it in DESIGN.md.
4. **Classify failures**: `PermanentError` (bad input, agent gave up) vs `TransientError` (throttle,
   timeout). Never subclass `TransientError`; Step Functions matches the class *name*.

## B. The five questions every step/operation must answer (write them in the PR/commit)

| Question | Required answer in this codebase |
|---|---|
| What is the **execution key**? | `(run_id, step_id)` for steps, `(run_id, request_id)` for human answers, `idempotency_key` for start |
| Where is "already done" **persisted**? | `STEP#<id>.status == DONE` (+ stored `result`), `REQ#<id>.status == ANSWERED` |
| What makes the write **atomic/conditional**? | one `TransactWriteItems`; conditions: step not DONE, run RUNNING, pointer == step |
| What happens on **retry after success**? | `begin_step` sees DONE → return stored result, **no re-execution** |
| What happens on **retry after crash mid-step**? | step is STARTED not DONE → it re-runs; its result was never committed, so nothing leaked |

If any answer is "check first, then write" without a condition, it is a bug.

## C. Rules for `store.py` changes

- A state change = conditional write. Use `models.allowed_sources(target)`; never hard-code sources.
- Result + pointer advance + status + effect + events go in **one transaction** (max 100 items).
- Use placeholders for **every** attribute name (`#status`, `#result`...). Many words are reserved.
- Floats: DynamoDB rejects them; always go through `_to_ddb` / `_from_ddb`.
- On `ConditionFailed`, the *engine* reloads and decides: duplicate (return success), stale (halt),
  or invalid (raise `InvalidTransition`). Do not retry blindly.

## D. Tests you must add (all of them)

1. **Happy path** through `engine` public API.
2. **Retry after success**: call `run_step` again → `reused`, effect count unchanged, `invocations` of
   earlier steps unchanged.
3. **Concurrent**: N threads on the same step/answer → exactly one effect (see `test_idempotency.py`).
4. **Invalid-source**: operation from a status that is not allowed → `InvalidTransition`.
5. **Guard in isolation** in `tests/test_store_guards.py`: rig persisted state so every *other* guard
   passes and only the new guard can reject. Engine-level tests alone miss redundant guards.
6. Run `python scripts/guard_mutation_check.py` (or `/guard-check`) and add a mutation for your new guard.

## E. Smells that mean "stop and redesign"

- A `get_item` followed by `put_item`/`update_item` with no condition.
- Anything stored only in memory, a Lambda global, or a Step Functions execution input that the run needs later.
- A step that writes to two places in two calls.
- Catching `Exception` to "make it pass".
- A test that only checks the happy path of a retry.
