import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from agent_runner import handlers
from agent_runner.engine import Engine
from agent_runner.orchestrator import LocalOrchestrator, StepFunctionsOrchestrator, execution_name
from conftest import LARGE, SMALL

ARN = "arn:aws:states:eu-west-1:123456789012:stateMachine:agent-runner"


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


# ---------------------------------------------------------------- StepFunctionsOrchestrator
def test_launch_uses_deterministic_execution_name_and_run_id_input():
    sfn = MagicMock()
    StepFunctionsOrchestrator(ARN, sfn).launch("run-abc", 3)
    sfn.start_execution.assert_called_once_with(
        stateMachineArn=ARN, name="run-abc-a3", input=json.dumps({"run_id": "run-abc"}))
    assert execution_name("run-abc", 3) == "run-abc-a3"


def test_launch_treats_existing_execution_as_success_but_raises_other_errors():
    sfn = MagicMock()
    sfn.start_execution.side_effect = client_error("ExecutionAlreadyExists")
    StepFunctionsOrchestrator(ARN, sfn).launch("run-abc", 1)  # idempotent: no raise
    sfn.start_execution.side_effect = client_error("AccessDeniedException")
    with pytest.raises(ClientError):
        StepFunctionsOrchestrator(ARN, sfn).launch("run-abc", 1)


def test_cancel_stops_the_execution_of_the_current_attempt():
    sfn = MagicMock()
    StepFunctionsOrchestrator(ARN, sfn).cancel("run-abc", 2)
    kwargs = sfn.stop_execution.call_args.kwargs
    assert kwargs["executionArn"] == "arn:aws:states:eu-west-1:123456789012:execution:agent-runner:run-abc-a2"
    sfn.stop_execution.side_effect = client_error("ExecutionDoesNotExist")
    StepFunctionsOrchestrator(ARN, sfn).cancel("run-abc", 2)  # already gone: fine


def test_deliver_sends_task_success_and_ignores_stale_tokens():
    sfn = MagicMock()
    o = StepFunctionsOrchestrator(ARN, sfn)
    o.deliver("r", "q", "TOK", {"decision": "approve"})
    sfn.send_task_success.assert_called_once_with(taskToken="TOK", output='{"decision": "approve"}')
    for code in ("TaskDoesNotExist", "InvalidToken", "TaskTimedOut"):
        sfn.send_task_success.side_effect = client_error(code)
        o.deliver("r", "q", "TOK", {"decision": "approve"})  # no raise
    sfn.send_task_success.side_effect = client_error("AccessDeniedException")
    with pytest.raises(ClientError):
        o.deliver("r", "q", "TOK", {"decision": "approve"})


def test_deliver_without_token_is_a_noop():
    sfn = MagicMock()
    StepFunctionsOrchestrator(ARN, sfn).deliver("r", "q", None, {"decision": "approve"})
    sfn.send_task_success.assert_not_called()


# ---------------------------------------------------------------- handlers
@pytest.fixture
def wired(store, agent):
    eng = Engine(store, agent, LocalOrchestrator())
    handlers.set_engine(eng)
    yield eng
    handlers.set_engine(None)


def test_control_handler_happy_path_and_error_mapping(wired):
    started = handlers.control({"op": "start", "args": {"input": LARGE}})
    assert started["ok"] and started["result"]["status"] == "WAITING_FOR_HUMAN"
    run_id = started["result"]["run_id"]
    pending = handlers.control({"op": "pending", "args": {"run_id": run_id}})["result"]
    bad = handlers.control({"op": "answer", "args": {"run_id": run_id, "request_id": "req-x", "decision": "approve"}})
    assert bad == {"ok": False, "error": {"type": "UnknownRequest", "message": bad["error"]["message"]}}
    ok = handlers.control({"op": "answer", "args": {"run_id": run_id, "request_id": pending["request_id"],
                                                    "decision": "approve"}})
    assert ok["ok"] and ok["result"]["status"] == "COMPLETED"
    again = handlers.control({"op": "resume", "args": {"run_id": run_id}})
    assert again["ok"] is False and again["error"]["type"] == "InvalidTransition"
    assert handlers.control({"op": "status", "args": {"run_id": "run-missing"}})["error"]["type"] == "RunNotFound"
    assert handlers.control({"op": "nope"})["error"]["type"] == "BadInput"


def test_worker_handler_route_run_step_and_mark_failed(idle_engine, store):
    eng, _ = idle_engine
    handlers.set_engine(eng)
    try:
        run_id = eng.start(SMALL)["run_id"]
        assert handlers.worker({"op": "route", "run_id": run_id}) == {"action": "run_step", "step_id": "validate_input"}
        assert handlers.worker({"op": "run_step", "run_id": run_id, "step_id": "validate_input"})["reused"] is False
        cause = json.dumps({"errorMessage": "boom", "errorType": "PermanentError"})
        out = handlers.worker({"op": "mark_failed", "run_id": run_id, "step_id": "agent_decision",
                               "error": {"Error": "PermanentError", "Cause": cause}})
        assert out == {"failed": True}
        st = eng.status(run_id)
        assert st["status"] == "FAILED" and st["error"]["message"] == "boom"
        assert handlers.worker({"op": "route", "run_id": run_id})["action"] == "halt"
    finally:
        handlers.set_engine(None)


def test_worker_raises_errors_by_exact_class_name_for_step_functions(idle_engine):
    """Step Functions Retry/Catch match on the Lambda errorType == exception class name."""
    eng, _ = idle_engine
    handlers.set_engine(eng)
    try:
        bad = eng.start({"customer": "", "amount": 1, "item": "x"})["run_id"]  # RUNNING; nothing executes
        with pytest.raises(Exception) as perm:
            handlers.worker({"op": "run_step", "run_id": bad, "step_id": "validate_input"})
        assert type(perm.value).__name__ == "PermanentError"

        flaky = eng.start({**SMALL, "simulate": {"step": "validate_input", "kind": "transient", "times": 1}})["run_id"]
        with pytest.raises(Exception) as trans:
            handlers.worker({"op": "run_step", "run_id": flaky, "step_id": "validate_input"})
        assert type(trans.value).__name__ == "TransientError"
    finally:
        handlers.set_engine(None)
