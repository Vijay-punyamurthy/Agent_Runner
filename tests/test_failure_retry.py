import pytest
from conftest import LARGE, SMALL

from agent_runner.errors import InvalidTransition


def steps(st):
    return {s["step_id"]: s for s in st["steps"]}


def with_sim(base, **sim):
    return {**base, "simulate": sim}


def test_permanent_failure_is_persisted_then_resume_continues_from_failed_step(engine, agent):
    run_id = engine.start(with_sim(SMALL, step="finalize", kind="permanent", until_attempt=1))["run_id"]
    st = engine.status(run_id)
    assert st["status"] == "FAILED"
    assert st["error"] == {"step": "finalize", "type": "PermanentError",
                           "message": "simulated permanent failure in finalize (attempt 1)"}
    assert st["current_step"] == "finalize" and st["effects"] == []
    assert steps(st)["validate_input"]["status"] == "DONE" and steps(st)["agent_decision"]["status"] == "DONE"

    out = engine.resume(run_id)
    assert out["attempt"] == 2
    st = engine.status(run_id)
    assert st["status"] == "COMPLETED" and st["error"] is None and len(st["effects"]) == 1
    # completed steps were NOT re-executed by the resume
    assert steps(st)["validate_input"]["invocations"] == 1
    assert steps(st)["agent_decision"]["invocations"] == 1 and agent.calls == 1
    assert steps(st)["finalize"]["invocations"] == 2


def test_failure_after_human_answer_does_not_ask_again_and_resumes(engine, agent):
    run_id = engine.start(with_sim(LARGE, step="finalize", kind="permanent", until_attempt=1))["run_id"]
    rid = engine.pending(run_id)["request_id"]
    engine.answer(run_id, rid, "approve")
    assert engine.status(run_id)["status"] == "FAILED"
    engine.resume(run_id)
    st = engine.status(run_id)
    assert st["status"] == "COMPLETED" and len(st["effects"]) == 1
    assert len(st["requests"]) == 1 and agent.calls == 1  # never re-asked
    # and a repeated answer is still a harmless duplicate
    assert engine.answer(run_id, rid, "approve")["duplicate"] is True
    assert len(engine.status(run_id)["effects"]) == 1


def test_transient_failures_are_retried_and_run_still_completes(engine):
    run_id = engine.start(with_sim(SMALL, step="agent_decision", kind="transient", times=2))["run_id"]
    st = engine.status(run_id)
    assert st["status"] == "COMPLETED"
    assert steps(st)["agent_decision"]["invocations"] == 3  # 2 transient failures + 1 success
    assert len(st["effects"]) == 1


def test_transient_failure_that_never_clears_ends_failed_then_resume_works(engine):
    run_id = engine.start(with_sim(SMALL, step="agent_decision", kind="transient", times=4))["run_id"]
    st = engine.status(run_id)
    assert st["status"] == "FAILED" and st["error"]["type"] == "TransientError"
    assert steps(st)["agent_decision"]["invocations"] == 4  # 1 try + 3 retries
    engine.resume(run_id)  # invocation 5 > times=4 => succeeds
    st = engine.status(run_id)
    assert st["status"] == "COMPLETED" and len(st["effects"]) == 1


def test_agent_failure_decision_maps_to_permanent_and_retryable_to_transient(store):
    from agent_runner.agent import Fail
    from agent_runner.engine import Engine
    from agent_runner.orchestrator import LocalOrchestrator

    class Flaky:
        n = 0

        def decide(self, ctx):
            Flaky.n += 1
            return Fail("llm timeout", retryable=True) if Flaky.n < 3 else Fail("policy violation")

    eng = Engine(store, Flaky(), LocalOrchestrator())
    run_id = eng.start(SMALL)["run_id"]
    st = eng.status(run_id)
    assert st["status"] == "FAILED" and st["error"]["type"] == "PermanentError"  # 2 retries then permanent
    assert Flaky.n == 3


def test_resume_clears_error_but_history_keeps_the_failure(engine):
    run_id = engine.start(with_sim(SMALL, step="finalize", kind="permanent"))["run_id"]
    engine.resume(run_id)
    types = [h["type"] for h in engine.status(run_id)["history"]]
    assert "RUN_FAILED" in types and "RUN_RESUMED" in types and types[-1] == "RUN_COMPLETED"


def test_cannot_stop_or_answer_a_failed_run_but_can_resume(engine):
    run_id = engine.start(with_sim(LARGE, step="finalize", kind="permanent"))["run_id"]
    rid = engine.pending(run_id)["request_id"]
    engine.answer(run_id, rid, "approve")
    with pytest.raises(InvalidTransition):
        engine.stop(run_id)  # FAILED -> STOPPED is not allowed
    engine.resume(run_id)
    assert engine.status(run_id)["status"] == "COMPLETED"
