import pytest
from conftest import LARGE, SMALL

from agent_runner.errors import InvalidTransition


def steps(st):
    return {s["step_id"]: s for s in st["steps"]}


def test_stop_waiting_run_preserves_pending_request(engine):
    run_id = engine.start(LARGE)["run_id"]
    rid = engine.pending(run_id)["request_id"]
    out = engine.stop(run_id)
    assert out["status"] == "STOPPED" and out["pending_request_id"] == rid
    st = engine.status(run_id)
    assert st["status"] == "STOPPED"
    assert st["pending_request"]["request_id"] == rid  # not lost
    assert steps(st)["validate_input"]["status"] == "DONE"


def test_cannot_answer_while_stopped_until_resumed(engine):
    run_id = engine.start(LARGE)["run_id"]
    rid = engine.pending(run_id)["request_id"]
    engine.stop(run_id)
    with pytest.raises(InvalidTransition):
        engine.answer(run_id, rid, "approve")
    assert engine.status(run_id)["effects"] == []


def test_resume_stopped_waiting_run_returns_to_waiting_same_request_no_rework(engine, agent):
    run_id = engine.start(LARGE)["run_id"]
    rid = engine.pending(run_id)["request_id"]
    engine.stop(run_id)
    out = engine.resume(run_id)
    assert out["status"] == "WAITING_FOR_HUMAN" and out["attempt"] == 2
    assert engine.pending(run_id)["request_id"] == rid
    # completed steps were not re-executed
    st = engine.status(run_id)
    assert steps(st)["validate_input"]["invocations"] == 1
    assert steps(st)["agent_decision"]["invocations"] == 1
    assert agent.calls == 1
    engine.answer(run_id, rid, "approve")
    st = engine.status(run_id)
    assert st["status"] == "COMPLETED" and len(st["effects"]) == 1 and agent.calls == 1


def test_full_demo_path_with_duplicate_answer_after_stop_resume(engine, agent):
    run_id = engine.start(LARGE)["run_id"]
    rid = engine.pending(run_id)["request_id"]
    engine.stop(run_id)
    engine.resume(run_id)
    engine.answer(run_id, rid, "approve")
    again = engine.answer(run_id, rid, "approve")
    assert again["duplicate"] is True
    st = engine.status(run_id)
    assert st["status"] == "COMPLETED" and len(st["effects"]) == 1
    assert [h["type"] for h in st["history"]].count("HUMAN_INPUT_RECEIVED") == 1


def test_stop_in_the_middle_of_a_step_discards_it_and_resume_redoes_only_that_step(store):
    """Operator stops while the agent step is executing. Its result must not be committed."""
    from agent_runner.agent import StubAgent
    from agent_runner.engine import Engine
    from agent_runner.orchestrator import LocalOrchestrator

    holder = {}

    class StoppingAgent:
        calls = 0

        def decide(self, context):
            StoppingAgent.calls += 1
            if StoppingAgent.calls == 1:
                holder["engine"].stop(holder["run_id"])  # operator hits stop mid-step
            return StubAgent().decide(context)

    eng = Engine(store, StoppingAgent(), LocalOrchestrator())
    holder["engine"] = eng
    # we need the run_id before the agent runs: pre-create via idempotency key
    run_id = "run-" + __import__("hashlib").sha256(b"k1").hexdigest()[:12]
    holder["run_id"] = run_id
    eng.start(SMALL, idempotency_key="k1")
    st = eng.status(run_id)
    assert st["status"] == "STOPPED"
    assert steps(st)["agent_decision"]["status"] == "STARTED"  # not committed
    assert st["effects"] == []
    eng.resume(run_id)
    st = eng.status(run_id)
    assert st["status"] == "COMPLETED" and len(st["effects"]) == 1
    assert steps(st)["validate_input"]["invocations"] == 1  # untouched
    assert steps(st)["agent_decision"]["invocations"] == 2  # re-run once, after resume


def test_stop_then_resume_a_run_stopped_after_answer_but_before_finalize(idle_engine):
    """Answer accepted (run RUNNING, finalize pending), then stop, then resume => finalize runs once."""
    from agent_runner.orchestrator import LocalOrchestrator
    eng, orch = idle_engine
    # drive manually: let the local loop run only up to the human wait
    local = LocalOrchestrator()
    local.bind(eng)
    run_id = eng.start(LARGE)["run_id"]
    local.drive(run_id)
    rid = eng.pending(run_id)["request_id"]
    eng.answer(run_id, rid, "approve")  # NoopOrchestrator.deliver: nothing runs
    assert eng.status(run_id)["status"] == "RUNNING"
    eng.stop(run_id)
    eng.resume(run_id)
    local.drive(run_id)
    st = eng.status(run_id)
    assert st["status"] == "COMPLETED" and len(st["effects"]) == 1


def test_stop_then_resume_on_stopped_run_midway(idle_engine):
    from agent_runner.orchestrator import LocalOrchestrator
    eng, _ = idle_engine
    local = LocalOrchestrator()
    local.bind(eng)
    run_id = eng.start(SMALL)["run_id"]  # RUNNING, nothing executed yet
    eng.stop(run_id)
    assert eng.status(run_id)["status"] == "STOPPED"
    eng.resume(run_id)
    local.drive(run_id)
    assert eng.status(run_id)["status"] == "COMPLETED"


def test_invalid_operations(engine, idle_engine):
    run_id = engine.start(SMALL)["run_id"]  # COMPLETED
    with pytest.raises(InvalidTransition):
        engine.resume(run_id)
    with pytest.raises(InvalidTransition):
        engine.stop(run_id)
    waiting = engine.start(LARGE)["run_id"]
    engine.stop(waiting)
    with pytest.raises(InvalidTransition):
        engine.stop(waiting)  # STOPPED -> STOP
    engine.resume(waiting)
    with pytest.raises(InvalidTransition):
        engine.resume(waiting)  # WAITING -> RESUME


def test_resume_running_run_is_rejected(idle_engine):
    eng, _ = idle_engine
    run_id = eng.start(SMALL)["run_id"]
    with pytest.raises(InvalidTransition):
        eng.resume(run_id)


def test_two_simultaneous_resumes_exactly_one_wins(idle_engine):
    import threading
    eng, orch = idle_engine
    run_id = eng.start(SMALL)["run_id"]
    eng.stop(run_id)
    orch.launched.clear()
    ok, bad = [], []

    def go():
        try:
            ok.append(eng.resume(run_id))
        except InvalidTransition as e:
            bad.append(e)

    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(ok) == 1 and len(bad) == 5
    assert len(orch.launched) == 1  # exactly one new execution
