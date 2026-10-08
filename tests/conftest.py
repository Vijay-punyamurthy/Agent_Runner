import threading

import pytest
from moto import mock_aws

from agent_runner.agent import Agent, StubAgent
from agent_runner.engine import Engine
from agent_runner.orchestrator import LocalOrchestrator
from agent_runner.store import Store


class CountingAgent:
    """Wraps an agent and counts decide() calls (to prove completed steps are not re-run)."""

    def __init__(self, inner: Agent | None = None):
        self.inner = inner or StubAgent(approval_threshold=1000)
        self.calls = 0
        self._lock = threading.Lock()

    def decide(self, context):
        with self._lock:
            self.calls += 1
        return self.inner.decide(context)


class NoopOrchestrator:
    """Accepts launches but runs nothing: leaves a run RUNNING with no worker (for transition tests)."""

    def __init__(self):
        self.launched, self.cancelled, self.delivered = [], [], []

    def launch(self, run_id, attempt):
        self.launched.append((run_id, attempt))

    def cancel(self, run_id, attempt):
        self.cancelled.append((run_id, attempt))

    def deliver(self, run_id, request_id, task_token, decision):
        self.delivered.append((run_id, request_id, task_token, decision))


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def _serialize_dynamodb_calls(store: Store) -> None:
    """Emulate DynamoDB's serializable per-request atomicity under threads.

    moto's in-process TransactWriteItems snapshots/restores the whole table on failure, so concurrent
    threads can roll back each other's committed writes (a moto limitation, not real DynamoDB
    behaviour). We serialize the *raw* DynamoDB calls with a lock. Races BETWEEN calls (read -> decide ->
    write in the engine) still interleave, which is what the concurrency tests exercise.
    """
    lock = threading.RLock()
    for name in ("transact_write_items", "update_item", "get_item", "query", "put_item"):
        original = getattr(store.client, name)

        def wrapped(*a, _orig=original, **kw):
            with lock:
                return _orig(*a, **kw)

        setattr(store.client, name, wrapped)


@pytest.fixture
def store(aws_env):
    with mock_aws():
        s = Store("agent-runner-test")
        s.create_table()
        _serialize_dynamodb_calls(s)
        yield s


@pytest.fixture
def agent():
    return CountingAgent()


@pytest.fixture
def engine(store, agent):
    return Engine(store, agent, LocalOrchestrator())


@pytest.fixture
def idle_engine(store, agent):
    """Engine whose orchestrator does nothing; returns (engine, orchestrator)."""
    orch = NoopOrchestrator()
    return Engine(store, agent, orch), orch


SMALL = {"customer": "Vijay", "amount": 500, "item": "Mouse"}
LARGE = {"customer": "Vijay", "amount": 5000, "item": "Laptop"}
