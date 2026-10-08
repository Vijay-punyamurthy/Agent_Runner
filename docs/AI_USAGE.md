# How I Worked with AI Tools

> Be honest and specific. Heavy use is great; we want to see how you stay in control.

> **AUTHOR'S NOTE - delete before submitting.** This file was drafted by the AI that did the build, and states only what
> happened in that session. Anything marked **TODO(you)** is something only you can write truthfully (your own review,
> your own runs of the skill/commands in Claude Code, your own reflection). Do not leave claims here that you cannot defend in the interview.

## Tools and models used

* **Claude (Anthropic), Claude Sonnet 5.5**, in the claude.ai chat interface with its code-execution sandbox (bash, file creation, web fetch, pip/npm).
  It read the assessment repo's README and templates via web fetch, wrote all code/docs in a sandbox clone, and ran tests there.
* **Not used in this build:** Claude Code, Codex, Copilot. (So the skill and slash commands below were *authored* for Claude Code but not yet *executed* through it. See their sections.)
* Tools executed in the sandbox: `pytest` + `moto` (DynamoDB / Step Functions mocks), `aws-cdk-lib` + the `aws-cdk` CLI (`cdk synth`), `ruff`.
* **TODO(you):** list anything you used yourself (editor assistant, other models, Claude Code during the interview prep).

## Workflow

1. **Prompt with an explicit process**: a long structured prompt told the AI to read the repo first, extract requirements, choose the architecture and write the state model *before* any code.
   The AI fetched `README.md`, `docs/DESIGN.md`, `docs/AI_USAGE.md`, `CLAUDE.md` and reported a requirements checklist + architecture + build plan; I approved it ("build the whole thing").
2. **Incremental phases, each committed** (see `git log`): scaffold + `CLAUDE.md` → domain model/agent/pipeline → DynamoDB store → engine + local orchestrator → tests (HITL, stop/resume, failure, idempotency, concurrency) →
   CLI + local demo → CDK stack → ASL-interpreter integration tests → skill/commands → docs.
3. **Run, don't assume**: tests ran after every phase (final: 90 passing); `cdk synth` run for real; the local demo script was executed end-to-end (every CLI call is a separate OS process).
4. **Adversarial verification**: after the tests passed, the AI *removed individual guards* from the code to see whether any test noticed (mutation testing). That found a real gap (see "Where the tools got it wrong" #2) and became `scripts/guard_mutation_check.py`.
5. No plan mode, sub-agents or parallel sessions were used; it was one linear conversation.
6. **TODO(you):** how you reviewed the code (what you read line by line, what you changed, what you questioned) and how you committed this into your repo (the sandbox commits are authored "Claude (build session)"; your repo's history should reflect your own commits).

## CLAUDE.md / AGENTS.md

Written in the **first commit, before any code**, so the agent had the architecture and invariants from the start: purpose, stack, file layout, nine invariants (DynamoDB is the source of truth; all state changes conditional;
result+pointer+status+effect in one transaction; `EFFECT#` with `attribute_not_exists`; DONE steps never re-run; `request_id` identity; durable wait; one start/resume path; error class names matter), commands, conventions, security rules.
At the end of the build it was updated with what was learned: the guard-isolation rule, the mutation script, the ASL test interpreter and the moto thread-safety caveat. `AGENTS.md` was not created (Claude only).
**TODO(you):** note how it evolved if you continue working with it.

## Skills

| Skill | Location | What it does | Why I created it |
| ----- | -------- | ------------ | ---------------- |
| `idempotent-step` | `.claude/skills/idempotent-step/SKILL.md` | Checklist for adding/changing a step or control operation without breaking retry/duplicate/resume safety: pure step function, effect-in-transaction, the "five questions" (execution key, where persisted, what's atomic, retry after success, retry after crash), store rules, mandatory test list, smells. | This is the hardest property of the system and the most likely thing to be broken by a future change (including by an AI during the live interview). Rule D5 ("test each guard in isolation") exists *because* of mistake #2 below. |

*Example of use* (a worked example, **not** a transcript of a Claude Code run): applying the five questions to `answer`:
key = `(run_id, request_id)`; "already done" = `REQ#<id>.status == ANSWERED`; atomic = one transaction (request PENDING→ANSWERED, run WAITING→RUNNING if `pending_request_id` matches, `human_approval` step DONE); retry after success = same decision → `duplicate: true`, nothing written;
retry after crash mid-way = the transaction either fully happened or not, and a repeated answer re-delivers a lost wake-up. Test D5 for it: `test_answer_rejected_if_request_not_pending` etc.
**TODO(you):** run the skill in Claude Code on a real change (e.g. "add a `notify` step with a side effect") and replace this with what happened.

## Slash commands

| Command | Location | What it does | When I used it |
| ------- | -------- | ------------ | -------------- |
| `/test-run` | `.claude/commands/test-run.md` | Runs the suite, root-causes failures, summarizes per area with real counts. | The same procedure (run `pytest`, diagnose, summarize) was done by hand many times in the build. The command file was written afterwards; **not yet run as a slash command**. |
| `/review` | `.claude/commands/review.md` | Skeptical review vs. the assessment: state, HITL, idempotency, stop/resume, failure, tests, security, docs, producing a `Requirement/Implemented/Tested/Documented` table. | Final review pass was done by hand against the README (see the checklist in the build report). **Not yet run as a slash command.** |
| `/guard-check` | `.claude/commands/guard-check.md` | Runs `scripts/guard_mutation_check.py`: removes each transaction guard / ASL wiring detail and requires a test to fail. | The underlying script **was run** (13/13 caught) and the manual version of it found mistake #2. |

*Examples:* `/guard-check` output → `13/13 mutations caught`. **TODO(you):** run `/test-run` and `/review` in Claude Code and paste a short real example for each.

## What I delegated vs did myself

| Part of the work | Delegated / Pair / Manual | Notes |
| ---------------- | ------------------------- | ----- |
| Requirements extraction, architecture and state model | Delegated, approved by me | AI proposed; trade-offs in DESIGN §7. **TODO(you):** what you changed or challenged. |
| Domain model, store, engine, orchestrator, handlers | Delegated | Verified by running tests, mutation checks and reading failures; not by trusting the generated code. |
| Test design (HITL, stop/resume, failure, idempotency, concurrency) | Delegated | Weak spots found by mutation testing, then fixed (mistake #2). |
| CLI + local demo | Delegated | Demo executed end-to-end in the sandbox. |
| CDK stack | Delegated | Verified with `cdk synth` + assertion tests + ASL interpreter; **not deployed**. |
| Real AWS deploy and verification | **Manual (TODO(you))** | Not possible from the build sandbox. |
| Docs (README, DESIGN, this file) | Delegated, then yours to edit | DESIGN/README claims checked against code; this file needs your edits. |
| Interview preparation / live extension | **Manual (TODO(you))** | |

## Where the tools got it wrong

1. **Assumed moto's DynamoDB transactions were atomic under threads.** The concurrent-answer test failed (run stayed `WAITING_FOR_HUMAN`, no errors raised). Instrumenting the run showed the request was marked `ANSWERED` but the run update was lost: moto snapshots and restores the whole table when a condition fails, clobbering other threads' committed writes.
   It is a moto limitation, not an engine bug (real DynamoDB transactions are serializable). Fix: the test fixture serializes raw DynamoDB calls with a lock, interleaving between the engine's read/decide/write steps still occurs; the caveat is documented in DESIGN §10 / README.
2. **A green test suite gave false confidence.** After 66 passing tests, deleting three guards from `commit_step` at once left every test green, because several guards overlap (for `finalize`, the `status = RUNNING` condition alone prevents a duplicate). The tests proved the property but would not notice a regressed guard.
   Fix: `tests/test_store_guards.py` rigs persisted state so only one guard can reject each write (10 tests), and `scripts/guard_mutation_check.py` removes 13 guards/wirings one at a time; all are caught.
3. **CDK stack had a dependency cycle and duplicate retry rules.** A redundant `machine.grant_task_response(worker)` made the worker's IAM policy depend on the machine, which depends on the worker (found by `Template.from_stack`: "Template is undeployable"). Also, my `RETRYABLE` list repeated Lambda service errors that CDK's `LambdaInvoke` already retries. Removed both; the infra test now looks up the retry rule by content instead of assuming index 0.
4. **Weak assertions in tests I wrote**: one used an `or` hedge that would have passed even if reuse regressed; another compared a counter to itself (`calls == calls`). Both caught on re-reading, replaced with exact assertions (including the exact state path the execution took: `Route → … → WaitForHuman`).
5. **Smaller slips caught by the tests/readback:** a convoluted expression (`allowed_sources(S.RUNNING) and [S.CREATED]`) in `start`; the router's `wait_human` result lacked a `step_id` that the state machine's `MarkFailed` path reads (noticed while writing the ASL, before any run).

## Reflection

* AI saved the most time on breadth: the store/engine/CDK/CLI plumbing, an exhaustive list of edge cases (stop mid-step, lost wake-up, answer-before-token), and keeping docs consistent with code.
* It cost time on environment realities (moto's non-atomic transactions under threads, CDK cycle) and on over-trusting passing tests. The most valuable habit was **trying to break the code** (mutations, a state-machine interpreter) rather than only running happy paths.
* What I'd do differently: deploy to real AWS in the first hour and make the AWS smoke test the first test, not the last; write the guard-isolation tests together with each guard instead of after the fact.
* **TODO(you):** your own reflection: what you'd change, what surprised you, where you disagreed with the AI.
