# Pausable Agent Runner on AWS

A small, durable runner for multi-step jobs where an "agent" step can decide it needs a human.
A run can **wait for a human for days with zero compute**, be **stopped** at any point (even while waiting),
and be **resumed** (after a stop or a failure) without redoing completed steps or repeating side effects.

> The assessment brief is in this repo's first commit (`git show $(git rev-list --max-parents=0 HEAD):README.md`).
> Design write-up: [`docs/DESIGN.md`](docs/DESIGN.md) · AI workflow: [`docs/AI_USAGE.md`](docs/AI_USAGE.md) · Agent guardrails: [`CLAUDE.md`](CLAUDE.md)

## How it works (30 seconds)

```
runner CLI ─► Control Lambda ─► DynamoDB  (source of truth: runs, steps, requests, effects, history)
                  │                ▲
                  ▼                │
          Step Functions (Standard): Route ─► RunStep ─┐
                  ▲                          WaitForHuman (.waitForTaskToken: parked, no compute)
                  └──────────────────────────────────┘
```
* Pipeline: `validate_input → agent_decision → [human_approval] → finalize` (trivial JSON work; the runner is the point).
* The stub agent asks for approval when `amount > 1000`. Swap it for an LLM by implementing `Agent.decide` (see DESIGN §6).
* Every state change is a conditional DynamoDB write; a step's result + pointer advance + side effect commit in **one transaction**.

## Prerequisites
Python 3.12 · Node.js 18+ (CDK CLI and jsii) · for AWS: an account, credentials in your environment/profile, `npx aws-cdk@2 bootstrap` once.

## Setup & test (no AWS needed)
```bash
make install
make test                                  # 90 tests (moto DynamoDB; CDK synth; state machine executed by a test interpreter)
python scripts/guard_mutation_check.py     # 13 guards removed one at a time; every one must make a test fail
make demo-local                            # full scripted story against a local moto DynamoDB server
```

## Deploy / demo / teardown (AWS)
```bash
export AWS_REGION=eu-west-1                # any region; nothing is hard-coded
make deploy                                # cdk deploy; writes cdk-outputs.json which the CLI reads
bash scripts/demo_aws.sh                   # start → pause → stop → resume → answer → completed (+ failure/resume)
make destroy                               # removes everything (table is RemovalPolicy.DESTROY)
```
Cost: on-demand DynamoDB, a few Lambda invocations, <4k state transitions → free tier.

## Commands
```bash
runner start '{"customer":"Vijay","amount":5000,"item":"Laptop"}' [--key order-42]   # --key makes start idempotent
runner status  <run_id> [--json]     # state, current step, pending request, per-step invocations, history
runner pending <run_id>              # the pending human request (id, question, options)
runner answer  <run_id> <request_id> approve|reject [--comment "..."]
runner stop    <run_id>              # RUNNING or WAITING_FOR_HUMAN -> STOPPED (state preserved)
runner resume  <run_id>              # STOPPED or FAILED -> continues, no completed step is re-run
```
Fault injection for demos/tests: add `"simulate": {"step":"finalize","kind":"permanent","until_attempt":1}` (fails on attempt 1, succeeds after `resume`)
or `{"step":"agent_decision","kind":"transient","times":2}` (succeeds after 2 retries).

### Example
```text
$ runner start '{"customer":"Vijay","amount":5000,"item":"Laptop"}'   -> run_id run-ab19...
$ runner status run-ab19...      -> WAITING_FOR_HUMAN, pending request req-b56e...  (nothing is running)
$ runner stop   run-ab19...      -> STOPPED  (request preserved)
$ runner answer run-ab19... req-b56e... approve   -> error: run is STOPPED ... resume it before answering
$ runner resume run-ab19...      -> WAITING_FOR_HUMAN again, SAME request, validate/agent not re-run
$ runner answer run-ab19... req-b56e... approve   -> COMPLETED, 1 side effect
$ runner answer run-ab19... req-b56e... approve   -> duplicate: true, still exactly 1 side effect
```

## Configuration
| Env var | Used by | Meaning |
|---|---|---|
| `RUNNER_MODE` | CLI | `aws` (default; invokes the control Lambda) or `local` (in-process) |
| `RUNNER_CONTROL_FUNCTION` | CLI | override the control Lambda name (default: from `cdk-outputs.json`, else `agent-runner-control`) |
| `RUNNER_TABLE`, `RUNNER_STATE_MACHINE_ARN` | Lambdas | set by CDK |
| `RUNNER_DDB_ENDPOINT` | local mode | DynamoDB endpoint (the demo points it at a local moto server) |
| `RUNNER_APPROVAL_THRESHOLD` | stub agent | amount above which a human is asked (default 1000) |

No secrets are used or stored. The local demo sets dummy AWS credentials at runtime only (moto ignores them).

## Known limitations (honest list)
* **Not verified on real AWS from the build environment**: no deployment was run. `cdk synth` succeeds, and the synthesized state machine is exercised by a test-only ASL interpreter against the real handlers; that is an approximation of Step Functions, not the service. First thing to do after deploying: run `scripts/demo_aws.sh`.
* If a Lambda dies between a state transition and `StartExecution`, the run can sit `RUNNING` with no execution (recover with `stop` then `resume`, or repeat `start --key`). A reconciler is described in DESIGN §9.
* Answers are rejected while a run is `STOPPED` (resume first). One request per run (ids derive from `run_id/step_id`).
* In-flight step results are discarded on stop (the step re-runs on resume) - a long LLM call may be paid twice.
* No auth, UI, multi-tenancy or production observability (out of scope; sketched in DESIGN §9).
* Test caveat: moto transactions aren't thread-safe, so concurrency tests serialize raw DynamoDB calls to emulate DynamoDB (DESIGN §10).
