"""Wiring from environment. No hard-coded account IDs, regions or ARNs."""
from __future__ import annotations

import os
from typing import Optional

from .agent import StubAgent
from .engine import Engine
from .orchestrator import LocalOrchestrator, StepFunctionsOrchestrator
from .store import Store


def build_engine(mode: Optional[str] = None) -> Engine:
    mode = mode or os.environ.get("RUNNER_MODE", "aws")
    store = Store(os.environ.get("RUNNER_TABLE", "agent-runner"),
                  endpoint_url=os.environ.get("RUNNER_DDB_ENDPOINT") or None)
    agent = StubAgent(float(os.environ.get("RUNNER_APPROVAL_THRESHOLD", "1000")))
    if mode == "local":
        return Engine(store, agent, LocalOrchestrator())
    return Engine(store, agent, StepFunctionsOrchestrator(os.environ["RUNNER_STATE_MACHINE_ARN"]))
