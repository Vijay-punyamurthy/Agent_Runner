"""Each guard in the store's transactions is tested IN ISOLATION.

The engine-level tests prove the end property (e.g. one side effect). But several guards overlap
(defense in depth), so removing one would go unnoticed there. Here we rig the persisted state so that
every OTHER guard passes and only the guard under test can reject the write.
"""
import pytest

from agent_runner.models import RunStatus as S
from agent_runner.store import ConditionFailed, pk

EFFECT = {"kind": "order_created", "customer": "V", "amount": 1, "item": "x"}


@pytest.fixture
def run(store):
    store.create_run("r1", {"customer": "V", "amount": 1, "item": "x"}, "finalize")
    store.transition("r1", S.RUNNING, sources=[S.CREATED], event="RUN_STARTED")
    return "r1"


def raw_update(store, sk, expr, names, values):
    store.table.update_item(Key={"PK": pk("r1"), "SK": sk}, UpdateExpression=expr,
                            ExpressionAttributeNames=names, ExpressionAttributeValues=values)


def commit_finalize(store, effect=EFFECT):
    store.commit_step("r1", "finalize", result={"outcome": "approved"}, next_step=None,
                      new_status=S.COMPLETED, effect=effect)


def test_commit_rejected_if_step_already_done(store, run):
    store.begin_step(run, "finalize")
    commit_finalize(store, effect=None)
    # rig: run looks RUNNING at the same pointer again, so ONLY the "step not DONE" guard can reject
    raw_update(store, "META", "SET #s = :r, #c = :f", {"#s": "status", "#c": "current_step"},
               {":r": "RUNNING", ":f": "finalize"})
    with pytest.raises(ConditionFailed):
        commit_finalize(store, effect=None)


def test_commit_rejected_if_pointer_moved(store, run):
    store.begin_step(run, "finalize")
    raw_update(store, "META", "SET #c = :a", {"#c": "current_step"}, {":a": "agent_decision"})
    with pytest.raises(ConditionFailed):  # step not DONE, run RUNNING; ONLY the pointer guard rejects
        commit_finalize(store, effect=None)


def test_commit_rejected_if_run_not_running(store, run):
    store.begin_step(run, "finalize")
    store.transition(run, S.STOPPED, sources=[S.RUNNING], event="RUN_STOPPED")
    with pytest.raises(ConditionFailed):
        commit_finalize(store, effect=None)
    assert store.get_step(run, "finalize")["status"] == "STARTED"  # nothing leaked through


def test_effect_cannot_be_recorded_twice(store, run):
    store.begin_step(run, "finalize")
    commit_finalize(store)
    # rig every other guard to pass again: step STARTED, run RUNNING at finalize
    raw_update(store, "STEP#finalize", "SET #s = :s", {"#s": "status"}, {":s": "STARTED"})
    raw_update(store, "META", "SET #s = :r, #c = :f", {"#s": "status", "#c": "current_step"},
               {":r": "RUNNING", ":f": "finalize"})
    with pytest.raises(ConditionFailed):  # ONLY the EFFECT attribute_not_exists guard remains
        commit_finalize(store)
    assert len(store.get_bundle(run)["effects"]) == 1


def test_failed_commit_is_atomic_nothing_partially_written(store, run):
    store.begin_step(run, "finalize")
    store.transition(run, S.STOPPED, sources=[S.RUNNING], event="RUN_STOPPED")
    before = store.get_bundle(run)
    with pytest.raises(ConditionFailed):
        commit_finalize(store)
    after = store.get_bundle(run)
    assert after["effects"] == [] and after["steps"] == before["steps"] and after["events"] == before["events"]


# ----------------------------------------------------------------- answer_request guards
@pytest.fixture
def waiting(store, run):
    store.begin_step(run, "agent_decision")
    raw_update(store, "META", "SET #c = :a", {"#c": "current_step"}, {":a": "agent_decision"})
    store.commit_step(run, "agent_decision", result={"decision": "human_input"}, next_step="human_approval",
                      new_status=S.WAITING_FOR_HUMAN,
                      request={"request_id": "req-1", "question": "ok?", "options": ["approve", "reject"]})
    assert store.get_run(run)["status"] == "WAITING_FOR_HUMAN"
    return run


def answer(store, decision="approve"):
    store.answer_request("r1", "req-1", decision, None, next_step="finalize", human_step="human_approval")


def test_answer_rejected_if_request_not_pending(store, waiting):
    raw_update(store, "REQ#req-1", "SET #s = :a", {"#s": "status"}, {":a": "ANSWERED"})
    with pytest.raises(ConditionFailed):  # run WAITING and STEP absent: ONLY the request guard rejects
        answer(store)


def test_answer_rejected_if_run_not_waiting(store, waiting):
    store.transition(waiting, S.STOPPED, sources=[S.WAITING_FOR_HUMAN], event="RUN_STOPPED")
    with pytest.raises(ConditionFailed):  # request PENDING, STEP absent: ONLY the run-status guard rejects
        answer(store)
    assert store.get_request("r1", "req-1")["status"] == "PENDING"  # atomic: request untouched


def test_answer_rejected_if_run_waiting_on_a_different_request(store, waiting):
    raw_update(store, "META", "SET #p = :x", {"#p": "pending_request_id"}, {":x": "req-other"})
    with pytest.raises(ConditionFailed):
        answer(store)


def test_answer_rejected_if_human_step_record_exists(store, waiting):
    store.table.put_item(Item={"PK": pk("r1"), "SK": "STEP#human_approval", "status": "DONE"})
    with pytest.raises(ConditionFailed):  # ONLY the STEP attribute_not_exists guard rejects
        answer(store)


def test_transition_honours_expected_version(store, run):
    v = store.get_run(run)["version"]
    store.transition(run, S.STOPPED, sources=[S.RUNNING], event="RUN_STOPPED")
    with pytest.raises(ConditionFailed):
        store.transition(run, S.RUNNING, sources=[S.STOPPED], event="RUN_RESUMED", expect_version=v)
