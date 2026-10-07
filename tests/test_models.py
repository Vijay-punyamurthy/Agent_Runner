import pytest

from agent_runner.errors import InvalidTransition
from agent_runner.models import TRANSITIONS, RunStatus as S, allowed_sources, assert_transition


def test_completed_is_terminal():
    assert TRANSITIONS[S.COMPLETED] == frozenset()


@pytest.mark.parametrize("src,dst", [
    (S.CREATED, S.RUNNING), (S.RUNNING, S.WAITING_FOR_HUMAN), (S.WAITING_FOR_HUMAN, S.RUNNING),
    (S.RUNNING, S.COMPLETED), (S.RUNNING, S.FAILED), (S.FAILED, S.RUNNING),
    (S.RUNNING, S.STOPPED), (S.WAITING_FOR_HUMAN, S.STOPPED), (S.STOPPED, S.RUNNING),
    (S.STOPPED, S.WAITING_FOR_HUMAN),
])
def test_valid_transitions(src, dst):
    assert_transition(src, dst)


@pytest.mark.parametrize("src,dst", [
    (S.COMPLETED, S.RUNNING), (S.STOPPED, S.STOPPED), (S.FAILED, S.STOPPED),
    (S.CREATED, S.COMPLETED), (S.WAITING_FOR_HUMAN, S.COMPLETED), (S.FAILED, S.WAITING_FOR_HUMAN),
])
def test_invalid_transitions(src, dst):
    with pytest.raises(InvalidTransition):
        assert_transition(src, dst)


def test_allowed_sources_derived_from_table():
    assert set(allowed_sources(S.STOPPED)) == {S.RUNNING, S.WAITING_FOR_HUMAN}
    assert set(allowed_sources(S.RUNNING)) == {S.CREATED, S.WAITING_FOR_HUMAN, S.STOPPED, S.FAILED}
