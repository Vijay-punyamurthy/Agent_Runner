"""A minimal Amazon States Language interpreter + fake `stepfunctions` client, for tests only.

It executes the REAL synthesized state machine definition against the REAL Lambda handlers, so we
verify the glue we can't deploy here: payload field names, Retry/Catch matching on the exception class
name, the waitForTaskToken park/wake handshake, stop semantics and execution-name idempotency.

Supported: Task (direct Lambda + lambda:invoke.waitForTaskToken), Choice(StringEquals), Succeed, Fail,
Retry, Catch, Parameters (.$ paths, $$.Task.Token), ResultPath. Retry back-off is not slept.
It is an approximation of Step Functions semantics, not the service; real-AWS verification is separate.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

from botocore.exceptions import ClientError


def _err(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


def _path(doc: dict, ctx: dict, p: str) -> Any:
    root, p = (ctx, p[2:]) if p.startswith("$$.") else (doc, p[2:] if p.startswith("$.") else "")
    for part in [x for x in p.split(".") if x]:
        root = root[part]
    return root


def _resolve(params: Any, doc: dict, ctx: dict) -> Any:
    if isinstance(params, dict):
        out = {}
        for k, v in params.items():
            if k.endswith(".$"):
                out[k[:-2]] = _path(doc, ctx, v)
            else:
                out[k] = _resolve(v, doc, ctx)
        return out
    return params


class Execution:
    def __init__(self, name: str, data: dict):
        self.name, self.data, self.status = name, data, "RUNNING"  # RUNNING|WAITING|SUCCEEDED|FAILED|ABORTED
        self.token: Optional[str] = None
        self.resume_state: Optional[str] = None
        self.visited: list[str] = []


class FakeStepFunctions:
    def __init__(self, definition: dict, lambda_handler: Callable[[dict], Any]):
        self.d, self.handler = definition, lambda_handler
        self.executions: dict[str, Execution] = {}
        self._n = 0
        self.invocations = 0  # lambda invocations (shows nothing runs while waiting)

    # ---- the subset of the boto3 client the orchestrator uses
    def start_execution(self, stateMachineArn: str, name: str, input: str) -> dict:
        ex = self.executions.get(name)
        if ex:
            if ex.status in ("RUNNING", "WAITING"):
                return {"executionArn": name}  # same name+input while running: idempotent
            raise _err("ExecutionAlreadyExists")
        ex = self.executions[name] = Execution(name, json.loads(input))
        self._run(ex, self.d["StartAt"])
        return {"executionArn": name}

    def stop_execution(self, executionArn: str, error: str = "", cause: str = "") -> dict:
        ex = self.executions.get(executionArn.split(":")[-1])
        if ex is None:
            raise _err("ExecutionDoesNotExist")
        if ex.status in ("RUNNING", "WAITING"):
            ex.status, ex.token = "ABORTED", None
        return {}

    def send_task_success(self, taskToken: str, output: str) -> dict:
        ex = next((e for e in self.executions.values() if e.token == taskToken and e.status == "WAITING"), None)
        if ex is None:
            raise _err("TaskDoesNotExist")
        nxt, ex.token, ex.status = ex.resume_state, None, "RUNNING"
        self._run(ex, nxt)
        return {}

    # ---- interpreter
    def _run(self, ex: Execution, state_name: str) -> None:
        states = self.d["States"]
        attempts: dict[str, int] = {}
        while True:
            if ex.status == "ABORTED":
                return
            st, ex.visited = states[state_name], ex.visited + [state_name]
            t = st["Type"]
            if t == "Succeed":
                ex.status = "SUCCEEDED"
                return
            if t == "Fail":
                ex.status = "FAILED"
                return
            if t == "Choice":
                state_name = next((c["Next"] for c in st["Choices"]
                                   if _path(ex.data, {}, c["Variable"]) == c["StringEquals"]), st["Default"])
                continue
            assert t == "Task", t
            waits = st["Resource"].endswith(".waitForTaskToken")
            ctx: dict = {}
            if waits:
                self._n += 1
                ctx = {"Task": {"Token": f"token-{ex.name}-{self._n}"}}
                ex.token, ex.resume_state, ex.status = ctx["Task"]["Token"], st["Next"], "WAITING"
            params = _resolve(st.get("Parameters", {}), ex.data, ctx)
            try:
                self.invocations += 1
                result = self.handler(params["Payload"] if waits else params)
            except Exception as e:  # noqa: BLE001 - Lambda reports errorType = class name
                name = type(e).__name__
                if waits:
                    ex.token, ex.status = None, "RUNNING"
                rule = next((r for r in st.get("Retry", []) if name in r["ErrorEquals"]), None)
                if rule and attempts.get(state_name, 0) < rule["MaxAttempts"]:
                    attempts[state_name] = attempts.get(state_name, 0) + 1
                    continue
                catch = next((c for c in st.get("Catch", [])
                              if name in c["ErrorEquals"] or "States.ALL" in c["ErrorEquals"]), None)
                if not catch:
                    ex.status = "FAILED"
                    return
                ex.data[catch["ResultPath"][2:]] = {"Error": name, "Cause": json.dumps(
                    {"errorMessage": str(e), "errorType": name})}
                state_name = catch["Next"]
                continue
            if waits:
                return  # parked (or already woken by a nested send_task_success): control leaves this call
            rp = st.get("ResultPath", "$")
            if rp not in (None, "$"):
                ex.data[rp[2:]] = result
            state_name = st["Next"]
