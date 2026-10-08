"""Pure step functions. No I/O: they return a StepOutcome that the engine commits atomically."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from . import agent as ag
from .errors import PermanentError, TransientError
from .models import AGENT, FINALIZE, VALIDATE


@dataclass
class HumanRequest:
    question: str
    options: tuple[str, ...]


@dataclass
class StepOutcome:
    result: dict[str, Any]
    human: Optional[HumanRequest] = None  # set => run pauses for a human after this step
    effect: Optional[dict[str, Any]] = None  # set => one recorded side effect (at-most-once)


def validate_input(context: Mapping[str, Any], agent: ag.Agent) -> StepOutcome:
    raw = context["input"]
    customer, item, amount = raw.get("customer"), raw.get("item"), raw.get("amount")
    if not isinstance(customer, str) or not customer.strip():
        raise PermanentError("input.customer must be a non-empty string")
    if not isinstance(item, str) or not item.strip():
        raise PermanentError("input.item must be a non-empty string")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
        raise PermanentError("input.amount must be a positive number")
    return StepOutcome(result={"customer": customer.strip(), "item": item.strip(), "amount": amount})


def agent_decision(context: Mapping[str, Any], agent: ag.Agent) -> StepOutcome:
    normalized = context["steps"][VALIDATE]
    decision = agent.decide({"input": normalized})
    if isinstance(decision, ag.Continue):
        return StepOutcome(result={"decision": "continue", "reason": decision.reason})
    if isinstance(decision, ag.HumanInput):
        return StepOutcome(
            result={"decision": "human_input", "question": decision.question},
            human=HumanRequest(decision.question, tuple(decision.options)),
        )
    if isinstance(decision, ag.Fail):
        raise (TransientError if decision.retryable else PermanentError)(decision.reason)
    raise PermanentError(f"agent returned unknown decision: {decision!r}")


def finalize(context: Mapping[str, Any], agent: ag.Agent) -> StepOutcome:
    data = context["steps"][VALIDATE]
    human = context.get("human")
    approved = human is None or human["decision"] == "approve"
    if not approved:
        return StepOutcome(result={"outcome": "rejected", "by": "human"})
    effect = {"kind": "order_created", **data, "approved_by": "human" if human else "auto"}
    return StepOutcome(result={"outcome": "approved"}, effect=effect)


STEP_FUNCTIONS: dict[str, Callable[[Mapping[str, Any], ag.Agent], StepOutcome]] = {
    VALIDATE: validate_input,
    AGENT: agent_decision,
    FINALIZE: finalize,
}
