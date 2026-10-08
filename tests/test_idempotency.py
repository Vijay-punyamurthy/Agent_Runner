import threading

from conftest import LARGE, SMALL

from agent_runner.engine import Engine
from agent_runner.orchestrator import LocalOrchestrator


def test_rerunning_a_completed_step_reuses_result_and_has_no_side_effect(engine, agent):
    run_id = engine.start(SMALL)["run_id"]
    before = engine.status(run_id)
    for step in ("validate_input", "agent_decision", "finalize"):
        assert engine.run_step(run_id, step)["halted"] is True  # run is COMPLETED, nothing executes
    after = engine.status(run_id)
    assert after["effects"] == before["effects"] and after["history"] == before["history"]
    assert agent.calls == 1


def test_run_step_on_done_step_returns_stored_result_without_executing(idle_engine, agent):
    eng, _ = idle_engine
    run_id = eng.start(SMALL)["run_id"]
    assert eng.run_step(run_id, "validate_input")["reused"] is False
    assert eng.run_step(run_id, "validate_input") == {"step_id": "validate_input", "reused": True}
    # agent step executed once even if the worker is invoked many times (Lambda retry)
    for _ in range(3):
        eng.run_step(run_id, "agent_decision")
    assert agent.calls == 1


def test_concurrent_workers_on_same_step_produce_one_side_effect(idle_engine):
    eng, _ = idle_engine
    run_id = eng.start(SMALL)["run_id"]
    eng.run_step(run_id, "validate_input")
    eng.run_step(run_id, "agent_decision")
    results, errors = [], []

    def worker():
        try:
            results.append(eng.run_step(run_id, "finalize"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    st = eng.status(run_id)
    assert st["status"] == "COMPLETED" and len(st["effects"]) == 1
    assert sum(1 for r in results if r.get("reused") is False) == 1  # exactly one winner


def test_duplicate_start_with_same_key_creates_one_run_and_one_effect(store, agent):
    eng = Engine(store, agent, LocalOrchestrator())
    a = eng.start(SMALL, idempotency_key="order-42")
    b = eng.start(SMALL, idempotency_key="order-42")
    assert a["run_id"] == b["run_id"] and a["duplicate"] is False and b["duplicate"] is True
    st = eng.status(a["run_id"])
    assert st["status"] == "COMPLETED" and len(st["effects"]) == 1 and agent.calls == 1


def test_duplicate_start_while_waiting_does_not_create_second_request(store, agent):
    eng = Engine(store, agent, LocalOrchestrator())
    a = eng.start(LARGE, idempotency_key="k")
    b = eng.start(LARGE, idempotency_key="k")
    st = eng.status(a["run_id"])
    assert b["duplicate"] is True and st["status"] == "WAITING_FOR_HUMAN"
    assert len(st["requests"]) == 1 and agent.calls == 1


def test_duplicate_start_relaunches_a_run_whose_first_launch_died(idle_engine):
    eng, orch = idle_engine
    eng.start(SMALL, idempotency_key="x")  # launch "lost"
    orch.launched.clear()
    again = eng.start(SMALL, idempotency_key="x")
    assert again["duplicate"] is True and len(orch.launched) == 1  # idempotent re-launch


def test_lost_wakeup_is_healed_by_repeating_the_answer(idle_engine):
    """Answer committed but the orchestrator wake-up was lost; a repeat answer re-delivers."""
    eng, orch = idle_engine
    local = LocalOrchestrator()
    local.bind(eng)
    run_id = eng.start(LARGE)["run_id"]
    local.drive(run_id)
    rid = eng.pending(run_id)["request_id"]
    eng.answer(run_id, rid, "approve")
    assert len(orch.delivered) == 1
    eng.answer(run_id, rid, "approve")  # run is RUNNING: redeliver
    assert len(orch.delivered) == 2
    local.drive(run_id)
    assert len(eng.status(run_id)["effects"]) == 1
    n = len(orch.delivered)
    eng.answer(run_id, rid, "approve")  # COMPLETED: no redelivery needed
    assert len(orch.delivered) == n


def test_register_wait_stores_token_and_wakes_if_answer_came_first(idle_engine):
    eng, orch = idle_engine
    local = LocalOrchestrator()
    local.bind(eng)
    run_id = eng.start(LARGE)["run_id"]
    local.drive(run_id)
    rid = eng.pending(run_id)["request_id"]
    # normal order: token registered, then answered -> token handed to orchestrator
    eng.register_wait(run_id, rid, "TOKEN-1")
    eng.answer(run_id, rid, "approve")
    assert orch.delivered[-1][2] == "TOKEN-1"
    assert "task_token" not in eng.status(run_id)["requests"][0]  # never exposed via status


def test_register_wait_after_answer_delivers_immediately(idle_engine):
    eng, orch = idle_engine
    local = LocalOrchestrator()
    local.bind(eng)
    run_id = eng.start(LARGE)["run_id"]
    local.drive(run_id)
    rid = eng.pending(run_id)["request_id"]
    eng.answer(run_id, rid, "approve")  # token not registered yet -> delivered with None
    assert orch.delivered[-1][2] is None
    eng.register_wait(run_id, rid, "TOKEN-LATE")  # state machine reaches the wait state late
    assert orch.delivered[-1][2] == "TOKEN-LATE"


def test_mark_failed_does_not_overwrite_stopped_or_completed(engine, idle_engine):
    run_id = engine.start(SMALL)["run_id"]
    assert engine.mark_failed(run_id, "finalize", "PermanentError", "late") == {"ignored": True}
    assert engine.status(run_id)["status"] == "COMPLETED"
