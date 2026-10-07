"""Real synthesized state machine definition + real handlers + real orchestrator adapter + moto DynamoDB."""
import json
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("aws_cdk")
if shutil.which("node") is None:
    pytest.skip("node not available for CDK synth", allow_module_level=True)

import aws_cdk as cdk  # noqa: E402
from aws_cdk.assertions import Template  # noqa: E402

from agent_runner import handlers  # noqa: E402
from agent_runner.engine import Engine  # noqa: E402
from agent_runner.orchestrator import StepFunctionsOrchestrator  # noqa: E402
from asl_sim import FakeStepFunctions  # noqa: E402
from conftest import LARGE, SMALL  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "infra"))
from stack import AgentRunnerStack  # noqa: E402

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:agent-runner-sm"


@pytest.fixture(scope="module")
def definition():
    t = Template.from_stack(AgentRunnerStack(cdk.App(), "T", stack_name="agent-runner"))
    sm = next(iter(t.find_resources("AWS::StepFunctions::StateMachine").values()))
    raw = sm["Properties"]["DefinitionString"]
    return json.loads("".join(p if isinstance(p, str) else "ARN" for p in raw["Fn::Join"][1]))


@pytest.fixture
def sfn_env(store, agent, definition):
    sfn = FakeStepFunctions(definition, handlers.worker)
    engine = Engine(store, agent, StepFunctionsOrchestrator(ARN, sfn))
    handlers.set_engine(engine)
    yield engine, sfn
    handlers.set_engine(None)


def test_small_run_executes_through_the_state_machine_and_succeeds(sfn_env):
    engine, sfn = sfn_env
    run_id = engine.start(SMALL)["run_id"]
    assert engine.status(run_id)["status"] == "COMPLETED"
    ex = sfn.executions[f"{run_id}-a1"]
    assert ex.status == "SUCCEEDED" and ex.visited[-1] == "Done"


def test_wait_is_durable_nothing_runs_while_waiting_then_answer_wakes_it(sfn_env):
    engine, sfn = sfn_env
    run_id = engine.start(LARGE)["run_id"]
    assert engine.status(run_id)["status"] == "WAITING_FOR_HUMAN"
    ex = sfn.executions[f"{run_id}-a1"]
    assert ex.status == "WAITING" and ex.visited[-1] == "WaitForHuman"
    # the execution parked at the wait state; it did not loop or poll
    assert ex.visited == ["Route", "NextAction", "RunStep", "Route", "NextAction", "RunStep",
                          "Route", "NextAction", "WaitForHuman"]
    engine.answer(run_id, engine.pending(run_id)["request_id"], "approve")
    st = engine.status(run_id)
    assert st["status"] == "COMPLETED" and len(st["effects"]) == 1
    assert ex.status == "SUCCEEDED"


def test_duplicate_answer_through_state_machine_has_no_second_effect(sfn_env):
    engine, sfn = sfn_env
    run_id = engine.start(LARGE)["run_id"]
    rid = engine.pending(run_id)["request_id"]
    engine.answer(run_id, rid, "approve")
    calls = sfn.invocations
    assert engine.answer(run_id, rid, "approve")["duplicate"] is True
    assert sfn.invocations == calls and len(engine.status(run_id)["effects"]) == 1


def test_stop_while_waiting_aborts_execution_and_resume_starts_new_one_on_same_request(sfn_env):
    engine, sfn = sfn_env
    run_id = engine.start(LARGE)["run_id"]
    rid = engine.pending(run_id)["request_id"]
    old_token = engine.store.get_request(run_id, rid)["task_token"]
    engine.stop(run_id)
    assert sfn.executions[f"{run_id}-a1"].status == "ABORTED"
    assert engine.status(run_id)["pending_request"]["request_id"] == rid

    engine.resume(run_id)
    new = sfn.executions[f"{run_id}-a2"]
    assert new.status == "WAITING"
    new_token = engine.store.get_request(run_id, rid)["task_token"]
    assert new_token != old_token  # fresh token parked on the SAME request
    # a stale wake-up with the old token must be harmless
    engine.orch.deliver(run_id, rid, old_token, {"decision": "approve"})
    assert engine.status(run_id)["status"] == "WAITING_FOR_HUMAN"

    engine.answer(run_id, rid, "approve")
    st = engine.status(run_id)
    assert st["status"] == "COMPLETED" and len(st["effects"]) == 1
    assert {s["step_id"]: s["invocations"] for s in st["steps"]}["agent_decision"] == 1


def test_permanent_failure_is_caught_to_mark_failed_and_resume_runs_a_new_execution(sfn_env):
    engine, sfn = sfn_env
    run_id = engine.start({**SMALL, "simulate": {"step": "finalize", "kind": "permanent", "until_attempt": 1}})["run_id"]
    st = engine.status(run_id)
    assert st["status"] == "FAILED" and st["error"]["type"] == "PermanentError"
    assert "simulated permanent failure" in st["error"]["message"]  # Cause JSON parsed by the handler
    assert st["error"]["step"] == "finalize"  # step_id flowed through the MarkFailed payload
    assert sfn.executions[f"{run_id}-a1"].status == "FAILED"
    engine.resume(run_id)
    st = engine.status(run_id)
    assert st["status"] == "COMPLETED" and len(st["effects"]) == 1
    assert {s["step_id"]: s["invocations"] for s in st["steps"]}["validate_input"] == 1


def test_transient_failures_are_retried_by_the_state_machine(sfn_env):
    engine, sfn = sfn_env
    run_id = engine.start({**SMALL, "simulate": {"step": "agent_decision", "kind": "transient", "times": 2}})["run_id"]
    st = engine.status(run_id)
    assert st["status"] == "COMPLETED"
    assert {s["step_id"]: s["invocations"] for s in st["steps"]}["agent_decision"] == 3


def test_transient_failure_beyond_retry_budget_marks_failed(sfn_env):
    engine, sfn = sfn_env
    run_id = engine.start({**SMALL, "simulate": {"step": "agent_decision", "kind": "transient", "times": 9}})["run_id"]
    st = engine.status(run_id)
    assert st["status"] == "FAILED" and st["error"]["type"] == "TransientError"
    assert {s["step_id"]: s["invocations"] for s in st["steps"]}["agent_decision"] == 4  # 1 + 3 retries


def test_duplicate_start_reuses_the_same_named_execution(sfn_env):
    engine, sfn = sfn_env
    a = engine.start(LARGE, idempotency_key="dup")
    b = engine.start(LARGE, idempotency_key="dup")
    assert a["run_id"] == b["run_id"] and len(sfn.executions) == 1
    assert len(engine.status(a["run_id"])["requests"]) == 1
