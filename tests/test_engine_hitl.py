import threading

import pytest
from conftest import LARGE

from agent_runner.errors import BadInput, Conflict, UnknownRequest


def waiting_run(engine):
    run_id = engine.start(LARGE)["run_id"]
    return run_id, engine.pending(run_id)["request_id"]


def test_approve_resumes_and_completes_using_the_answer(engine):
    run_id, rid = waiting_run(engine)
    out = engine.answer(run_id, rid, "approve", comment="ok by finance")
    assert out["duplicate"] is False and out["status"] == "COMPLETED"
    st = engine.status(run_id)
    assert [e["kind"] for e in st["effects"]] == ["order_created"]
    assert st["effects"][0]["approved_by"] == "human"
    req = st["requests"][0]
    assert req["status"] == "ANSWERED" and req["decision"] == "approve" and req["comment"] == "ok by finance"
    assert st["pending_request"] is None


def test_reject_completes_without_side_effect(engine):
    run_id, rid = waiting_run(engine)
    engine.answer(run_id, rid, "reject")
    st = engine.status(run_id)
    assert st["status"] == "COMPLETED" and st["effects"] == []
    assert [s for s in st["steps"] if s["step_id"] == "finalize"][0]["result"]["outcome"] == "rejected"


def test_duplicate_answer_does_not_repeat_side_effect(engine):
    run_id, rid = waiting_run(engine)
    first = engine.answer(run_id, rid, "approve")
    before = engine.status(run_id)
    second = engine.answer(run_id, rid, "approve")
    after = engine.status(run_id)
    assert first["duplicate"] is False and second["duplicate"] is True
    assert len(after["effects"]) == 1
    assert after["history"] == before["history"]  # nothing new was recorded
    assert after["status"] == "COMPLETED"


def test_conflicting_second_answer_is_rejected(engine):
    run_id, rid = waiting_run(engine)
    engine.answer(run_id, rid, "approve")
    with pytest.raises(Conflict):
        engine.answer(run_id, rid, "reject")
    assert len(engine.status(run_id)["effects"]) == 1


def test_simultaneous_duplicate_answers_have_exactly_one_effect(engine):
    run_id, rid = waiting_run(engine)
    results, errors = [], []

    def go():
        try:
            results.append(engine.answer(run_id, rid, "approve"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    assert sum(1 for r in results if not r["duplicate"]) == 1
    st = engine.status(run_id)
    assert st["status"] == "COMPLETED" and len(st["effects"]) == 1


def test_wrong_request_id_rejected_and_run_keeps_waiting(engine):
    run_id, rid = waiting_run(engine)
    with pytest.raises(UnknownRequest):
        engine.answer(run_id, "req-nope", "approve")
    assert engine.status(run_id)["status"] == "WAITING_FOR_HUMAN"


def test_invalid_decision_value_rejected(engine):
    run_id, rid = waiting_run(engine)
    with pytest.raises(BadInput):
        engine.answer(run_id, rid, "maybe")


def test_answer_for_a_different_run_is_rejected(engine):
    run_a, rid_a = waiting_run(engine)
    run_b, _ = waiting_run(engine)
    # request ids are derived per run+step, so run_a's id must not resolve on run_b
    assert rid_a != engine.pending(run_b)["request_id"]
    with pytest.raises(UnknownRequest):
        engine.answer(run_b, rid_a, "approve")


def test_answer_on_run_that_never_asked_is_rejected(engine):
    from conftest import SMALL
    run_id = engine.start(SMALL)["run_id"]
    with pytest.raises(UnknownRequest):
        engine.answer(run_id, "req-whatever", "approve")
