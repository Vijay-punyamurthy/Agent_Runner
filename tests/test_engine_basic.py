from conftest import LARGE, SMALL


def step_statuses(st):
    return {s["step_id"]: s["status"] for s in st["steps"]}


def test_small_run_completes_in_order_without_human(engine, agent):
    run_id = engine.start(SMALL)["run_id"]
    st = engine.status(run_id)
    assert st["status"] == "COMPLETED"
    assert step_statuses(st) == {"validate_input": "DONE", "agent_decision": "DONE",
                                 "human_approval": "SKIPPED", "finalize": "DONE"}
    completed = [h["step"] for h in st["history"] if h["type"] == "STEP_COMPLETED"]
    assert completed == ["validate_input", "agent_decision", "finalize"]
    assert len(st["effects"]) == 1 and st["effects"][0]["kind"] == "order_created"
    assert st["pending_request"] is None
    assert agent.calls == 1


def test_large_run_pauses_durably_for_human(engine):
    run_id = engine.start(LARGE)["run_id"]
    st = engine.status(run_id)
    assert st["status"] == "WAITING_FOR_HUMAN"
    assert st["current_step"] == "human_approval"
    p = st["pending_request"]
    assert p["request_id"].startswith("req-") and "Laptop" in p["question"]
    assert p["status"] == "PENDING" and "task_token" not in p
    assert st["effects"] == []  # nothing happened yet
    assert engine.pending(run_id)["request_id"] == p["request_id"]


def test_status_is_reconstructed_from_storage_only(store, agent):
    """A brand-new engine/process sees the same run: nothing lives in memory."""
    from agent_runner.engine import Engine
    from agent_runner.orchestrator import LocalOrchestrator
    run_id = Engine(store, agent, LocalOrchestrator()).start(LARGE)["run_id"]
    fresh = Engine(store, agent, LocalOrchestrator())
    assert fresh.status(run_id)["status"] == "WAITING_FOR_HUMAN"
    rid = fresh.pending(run_id)["request_id"]
    fresh.answer(run_id, rid, "approve")
    assert fresh.status(run_id)["status"] == "COMPLETED"


def test_bad_input_fails_permanently_at_validation(engine):
    run_id = engine.start({"customer": "", "amount": -1, "item": "x"})["run_id"]
    st = engine.status(run_id)
    assert st["status"] == "FAILED"
    assert st["error"]["step"] == "validate_input" and st["error"]["type"] == "PermanentError"
