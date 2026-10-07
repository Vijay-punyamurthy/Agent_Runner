"""Fault injection for the demo and tests (driven by `input.simulate`).

  {"simulate": {"step": "finalize", "kind": "permanent", "until_attempt": 1}}
      fails while the run's attempt number <= until_attempt (default 1), so `resume` fixes it.
  {"simulate": {"step": "agent_decision", "kind": "transient", "times": 2}}
      fails the first N invocations of that step, then succeeds (exercises retry).
"""
from __future__ import annotations

from typing import Any, Mapping

from .errors import PermanentError, TransientError


def maybe_fail(run_input: Mapping[str, Any], step_id: str, run_attempt: int, step_invocations: int) -> None:
    sim = run_input.get("simulate") if isinstance(run_input, Mapping) else None
    if not sim or sim.get("step") != step_id:
        return
    if sim.get("kind") == "transient" and step_invocations <= int(sim.get("times", 1)):
        raise TransientError(f"simulated transient failure in {step_id} (invocation {step_invocations})")
    if sim.get("kind") == "permanent" and run_attempt <= int(sim.get("until_attempt", 1)):
        raise PermanentError(f"simulated permanent failure in {step_id} (attempt {run_attempt})")
