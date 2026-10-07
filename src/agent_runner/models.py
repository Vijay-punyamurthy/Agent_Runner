"""Run / step state model. The transition table here is the single source of truth."""
from __future__ import annotations

from enum import Enum

from .errors import InvalidTransition


class RunStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


class StepStatus(str, Enum):
    STARTED = "STARTED"
    DONE = "DONE"


class RequestStatus(str, Enum):
    PENDING = "PENDING"
    ANSWERED = "ANSWERED"


S = RunStatus

# source -> allowed targets
TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    S.CREATED: frozenset({S.RUNNING}),
    S.RUNNING: frozenset({S.WAITING_FOR_HUMAN, S.COMPLETED, S.FAILED, S.STOPPED}),
    S.WAITING_FOR_HUMAN: frozenset({S.RUNNING, S.STOPPED}),
    # Resuming a run that was stopped while waiting returns it to WAITING (same request).
    S.STOPPED: frozenset({S.RUNNING, S.WAITING_FOR_HUMAN}),
    S.FAILED: frozenset({S.RUNNING}),
    S.COMPLETED: frozenset(),
}

TERMINAL = frozenset({S.COMPLETED})

# Pipeline. `human_approval` is a logical step only entered when the agent asks.
VALIDATE = "validate_input"
AGENT = "agent_decision"
HUMAN = "human_approval"
FINALIZE = "finalize"
STEP_ORDER = (VALIDATE, AGENT, HUMAN, FINALIZE)

DECISIONS = ("approve", "reject")


def allowed_sources(target: RunStatus) -> list[RunStatus]:
    return [src for src, targets in TRANSITIONS.items() if target in targets]


def assert_transition(src: RunStatus, dst: RunStatus) -> None:
    if dst not in TRANSITIONS[src]:
        raise InvalidTransition(f"cannot go from {src.value} to {dst.value}")
