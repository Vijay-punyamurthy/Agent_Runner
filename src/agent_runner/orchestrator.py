"""Orchestrator port: how the engine starts/cancels executions and wakes a waiting run.

* StepFunctionsOrchestrator - production. Wait = waitForTaskToken, so no compute while waiting.
* LocalOrchestrator         - same `route -> run_step` loop, in-process. State is still only in
                              DynamoDB, so a "pause" is just the loop returning.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Mapping, Optional, Protocol

import boto3
from botocore.exceptions import ClientError

from .errors import PermanentError, TransientError

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

log = logging.getLogger(__name__)


class Orchestrator(Protocol):
    def launch(self, run_id: str, attempt: int) -> None: ...
    def cancel(self, run_id: str, attempt: int) -> None: ...
    def deliver(self, run_id: str, request_id: str, task_token: Optional[str], decision: Mapping[str, Any]) -> None: ...


def execution_name(run_id: str, attempt: int) -> str:
    # Deterministic: StartExecution with the same name+input is idempotent in Step Functions.
    return f"{run_id}-a{attempt}"


class StepFunctionsOrchestrator:
    _IGNORED_DELIVER = {"TaskDoesNotExist", "InvalidToken", "TaskTimedOut"}
    _IGNORED_STOP = {"ExecutionDoesNotExist"}

    def __init__(self, state_machine_arn: str, client: Any = None):
        self.arn = state_machine_arn
        self.sfn = client or boto3.client("stepfunctions")

    def _execution_arn(self, run_id: str, attempt: int) -> str:
        return self.arn.replace(":stateMachine:", ":execution:") + ":" + execution_name(run_id, attempt)

    def launch(self, run_id: str, attempt: int) -> None:
        try:
            self.sfn.start_execution(stateMachineArn=self.arn, name=execution_name(run_id, attempt),
                                     input=json.dumps({"run_id": run_id}))
        except ClientError as e:
            # Same name already exists (duplicate start / retry): that IS the idempotent outcome.
            if e.response["Error"]["Code"] != "ExecutionAlreadyExists":
                raise

    def cancel(self, run_id: str, attempt: int) -> None:
        try:
            self.sfn.stop_execution(executionArn=self._execution_arn(run_id, attempt),
                                    error="OperatorStop", cause="stopped by operator")
        except ClientError as e:
            if e.response["Error"]["Code"] not in self._IGNORED_STOP:
                raise

    def deliver(self, run_id: str, request_id: str, task_token: Optional[str], decision: Mapping[str, Any]) -> None:
        if not task_token:
            # Token not registered yet: register_wait() will see the ANSWERED request and deliver.
            return
        try:
            self.sfn.send_task_success(taskToken=task_token, output=json.dumps(dict(decision)))
        except ClientError as e:
            # Already delivered / execution gone: harmless (state is in DynamoDB).
            if e.response["Error"]["Code"] not in self._IGNORED_DELIVER:
                raise
            log.info("send_task_success ignored: %s", e.response["Error"]["Code"])


class LocalOrchestrator:
    """Runs the same loop the state machine runs. Retries mirror the state machine's Retry policy."""

    MAX_RETRIES = 3
    MAX_STEPS_GUARD = 50

    def __init__(self) -> None:
        self.engine: Optional["Engine"] = None

    def bind(self, engine: "Engine") -> None:
        self.engine = engine

    def launch(self, run_id: str, attempt: int) -> None:
        self.drive(run_id)

    def cancel(self, run_id: str, attempt: int) -> None:
        return None  # drive() re-reads DynamoDB before every step; a stopped run halts there.

    def deliver(self, run_id: str, request_id: str, task_token: Optional[str], decision: Mapping[str, Any]) -> None:
        self.drive(run_id)

    def drive(self, run_id: str) -> None:
        assert self.engine is not None
        for _ in range(self.MAX_STEPS_GUARD):
            action = self.engine.route(run_id)
            if action["action"] != "run_step":
                return  # done / waiting for human (durable pause) / halted
            step_id = action["step_id"]
            if not self._run_with_retry(run_id, step_id):
                return

    def _run_with_retry(self, run_id: str, step_id: str) -> bool:
        assert self.engine is not None
        retries = 0
        while True:
            try:
                self.engine.run_step(run_id, step_id)
                return True
            except TransientError as e:
                if retries < self.MAX_RETRIES:
                    retries += 1
                    continue
                self.engine.mark_failed(run_id, step_id, "TransientError", f"retries exhausted: {e}")
                return False
            except PermanentError as e:
                self.engine.mark_failed(run_id, step_id, "PermanentError", str(e))
                return False
            except Exception as e:  # noqa: BLE001 - mirrors Catch States.ALL
                self.engine.mark_failed(run_id, step_id, type(e).__name__, str(e))
                return False
