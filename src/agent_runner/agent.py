"""Agent seam. Orchestration only ever talks to `Agent.decide`.

To plug in a real LLM: implement `Agent.decide(context)` (call the model, map its output to one of
the three decisions). Because the engine persists the decision once and reuses it on retry, a
non-deterministic LLM call is safe: it is made at most once per committed step.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Union


@dataclass(frozen=True)
class Continue:
    reason: str = ""


@dataclass(frozen=True)
class HumanInput:
    question: str
    options: tuple[str, ...] = ("approve", "reject")


@dataclass(frozen=True)
class Fail:
    reason: str
    retryable: bool = False


AgentDecision = Union[Continue, HumanInput, Fail]


class Agent(Protocol):
    def decide(self, context: Mapping[str, Any]) -> AgentDecision: ...


class StubAgent:
    """Deterministic stand-in for an LLM: asks for approval above a threshold."""

    def __init__(self, approval_threshold: float = 1000):
        self.approval_threshold = approval_threshold

    def decide(self, context: Mapping[str, Any]) -> AgentDecision:
        data = context["input"]
        if data["amount"] > self.approval_threshold:
            return HumanInput(
                question=(
                    f"Approve purchase of {data['item']} for {data['customer']} "
                    f"(amount {data['amount']})?"
                )
            )
        return Continue(reason=f"amount {data['amount']} <= {self.approval_threshold}")
