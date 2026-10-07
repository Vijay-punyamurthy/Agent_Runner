import os
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


@pytest.fixture
def store(aws_env):
    with mock_aws():
        s = Store("agent-runner-test")
        s.create_table()
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
