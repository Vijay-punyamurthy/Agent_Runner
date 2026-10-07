"""Control operations (start/stop/resume/answer/status) and worker operations (route/run_step/...).

Orchestration only. Business logic lives in pipeline.py, agent logic behind agent.Agent,
persistence in store.py.
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any, Mapping, Optional

from . import faults, pipeline
from .agent import Agent
from .errors import (BadInput, Conflict, InvalidTransition, PermanentError, RunNotFound, RunNotRunning,
                     TransientError, UnknownRequest)
from .models import (AGENT, DECISIONS, FINALIZE, HUMAN, STEP_ORDER, VALIDATE, RequestStatus, RunStatus,
                     StepStatus, allowed_sources, assert_transition)
from .orchestrator import Orchestrator
from .store import ConditionFailed, Store

S = RunStatus


def request_id_for(run_id: str, step_id: str) -> str:
    """Stable id derived from run+step, so concurrent workers agree on it."""
    return "req-" + hashlib.sha256(f"{run_id}/{step_id}".encode()).hexdigest()[:10]


class Engine:
    def __init__(self, store: Store, agent: Agent, orchestrator: Orchestrator):
        self.store, self.agent, self.orch = store, agent, orchestrator
        if hasattr(orchestrator, "bind"):
            orchestrator.bind(self)  # type: ignore[attr-defined]

    # ============================================================== control operations
    def start(self, run_input: Mapping[str, Any], idempotency_key: Optional[str] = None) -> dict:
        if not isinstance(run_input, Mapping):
            raise BadInput("input must be a JSON object")
        suffix = hashlib.sha256(idempotency_key.encode()).hexdigest()[:12] if idempotency_key else uuid.uuid4().hex[:12]
        run_id = f"run-{suffix}"
        created = self.store.create_run(run_id, run_input, VALIDATE, idempotency_key)
        run = self._must_get(run_id)
        if run["status"] in (S.CREATED.value, S.RUNNING.value):
            # New run, or duplicate start whose first attempt may have died before launching.
            # CREATED->RUNNING is conditional (only one caller wins); launch is idempotent
            # (deterministic execution name).
            if run["status"] == S.CREATED.value:
                assert_transition(S.CREATED, S.RUNNING)
                try:
                    self.store.transition(run_id, S.RUNNING, sources=[S.CREATED], event="RUN_STARTED")
                except ConditionFailed:
                    pass
            self.orch.launch(run_id, run["attempt"])
        out = self._must_get(run_id)
        return {"run_id": run_id, "status": out["status"], "duplicate": not created}

    def stop(self, run_id: str) -> dict:
        self._must_get(run_id)
        try:
            self.store.transition(run_id, S.STOPPED, sources=allowed_sources(S.STOPPED), event="RUN_STOPPED")
        except ConditionFailed:
            cur = self._must_get(run_id)
            raise InvalidTransition(f"cannot stop a run that is {cur['status']}") from None
        run = self._must_get(run_id)
        self.orch.cancel(run_id, run["attempt"])
        return {"run_id": run_id, "status": run["status"], "pending_request_id": run.get("pending_request_id")}

    def resume(self, run_id: str) -> dict:
        run = self._must_get(run_id)
        cur = S(run["status"])
        if cur not in (S.STOPPED, S.FAILED):
            raise InvalidTransition(f"cannot resume a run that is {cur.value} (only STOPPED or FAILED)")
        # A run stopped while waiting goes back to waiting on the SAME request; otherwise RUNNING.
        target = S.WAITING_FOR_HUMAN if (cur is S.STOPPED and run.get("pending_request_id")) else S.RUNNING
        assert_transition(cur, target)
        attempt = run["attempt"] + 1
        try:
            # expect_version: the decision above was made on this snapshot; if anything changed, reject.
            self.store.transition(run_id, target, sources=[cur], expect_version=run["version"], event="RUN_RESUMED",
                                  set_attrs={"attempt": attempt}, remove_attrs=("error", "failed_step"),
                                  detail={"from": cur.value, "attempt": attempt})
        except ConditionFailed:
            now = self._must_get(run_id)
            raise InvalidTransition(f"run changed concurrently; it is now {now['status']}") from None
        self.orch.launch(run_id, attempt)
        out = self._must_get(run_id)
        return {"run_id": run_id, "status": out["status"], "attempt": attempt}

    def answer(self, run_id: str, request_id: str, decision: str, comment: Optional[str] = None) -> dict:
        if decision not in DECISIONS:
            raise BadInput(f"decision must be one of {list(DECISIONS)}")
        self._must_get(run_id)
        if self.store.get_request(run_id, request_id) is None:
            raise UnknownRequest(f"no request {request_id!r} on run {run_id}")
        try:
            self.store.answer_request(run_id, request_id, decision, comment, next_step=FINALIZE, human_step=HUMAN)
            duplicate = False
        except ConditionFailed:
            req = self.store.get_request(run_id, request_id)
            if req and req["status"] == RequestStatus.ANSWERED.value:
                if req["decision"] != decision:
                    raise Conflict(f"request {request_id} already answered with {req['decision']!r}") from None
                duplicate = True  # same answer again: no new side effects; maybe heal a lost wake-up below
            else:
                cur = self._must_get(run_id)
                raise InvalidTransition(
                    f"run is {cur['status']}, not WAITING_FOR_HUMAN; resume it before answering") from None
        req = self.store.get_request(run_id, request_id) or {}
        run = self._must_get(run_id)
        if not duplicate or run["status"] == S.RUNNING.value:
            # First answer, or a repeat while RUNNING (the wake-up may have been lost): re-deliver.
            # Delivery is idempotent: waking an already-woken run is a no-op.
            self.orch.deliver(run_id, request_id, req.get("task_token"), {"decision": decision})
        return {"run_id": run_id, "request_id": request_id, "decision": decision, "duplicate": duplicate,
                "status": self._must_get(run_id)["status"]}

    def status(self, run_id: str) -> dict:
        b = self.store.get_bundle(run_id)
        if b is None:
            raise RunNotFound(f"no run {run_id}")
        run, steps, reqs = b["run"], b["steps"], b["requests"]
        agent_res = (steps.get(AGENT) or {}).get("result") or {}
        step_view = []
        for sid in STEP_ORDER:
            rec = steps.get(sid)
            if rec:
                st = rec["status"]
            elif sid == HUMAN and agent_res.get("decision") == "continue":
                st = "SKIPPED"
            else:
                st = "PENDING"
            step_view.append({"step_id": sid, "status": st, "invocations": (rec or {}).get("invocations", 0),
                              "result": (rec or {}).get("result")})
        clean = lambda r: {k: v for k, v in r.items() if k not in ("PK", "SK", "task_token")}  # noqa: E731
        pending = [clean(r) for r in reqs.values() if r["status"] == RequestStatus.PENDING.value]
        return {
            "run_id": run_id, "status": run["status"], "current_step": run.get("current_step"),
            "attempt": run["attempt"], "version": run["version"], "input": run["input"],
            "steps": step_view, "pending_request": pending[0] if pending else None,
            "requests": [clean(r) for r in reqs.values()], "error": run.get("error"),
            "effects": [clean(e) for e in b["effects"]],
            "history": [{"at": e["at"], "type": e["type"], **e.get("detail", {})} for e in b["events"]],
            "created_at": run["created_at"], "updated_at": run["updated_at"],
        }

    def pending(self, run_id: str) -> Optional[dict]:
        return self.status(run_id)["pending_request"]

    # ============================================================== worker operations
    def route(self, run_id: str) -> dict:
        """What should the executor do next? Reads DynamoDB only (same path for start AND resume)."""
        run = self.store.get_run(run_id)
        if run is None:
            raise PermanentError(f"run {run_id} not found")
        st = run["status"]
        if st == S.WAITING_FOR_HUMAN.value:
            return {"action": "wait_human", "step_id": HUMAN, "request_id": run["pending_request_id"]}
        if st == S.RUNNING.value and run.get("current_step"):
            return {"action": "run_step", "step_id": run["current_step"]}
        if st == S.COMPLETED.value:
            return {"action": "done"}
        return {"action": "halt", "status": st}

    def run_step(self, run_id: str, step_id: str) -> dict:
        """Execute one step at-most-once-effect. Safe to call repeatedly / concurrently."""
        run = self.store.get_run(run_id)
        if run is None:
            raise PermanentError(f"run {run_id} not found")
        if run["status"] != S.RUNNING.value:
            return {"halted": True, "status": run["status"]}
        if step_id not in pipeline.STEP_FUNCTIONS:
            raise PermanentError(f"unknown step {step_id!r}")

        rec = self.store.begin_step(run_id, step_id)  # also counts the invocation
        if rec["status"] == StepStatus.DONE.value:
            return {"step_id": step_id, "reused": True}  # completed steps never re-execute
        if run.get("current_step") != step_id:
            return {"stale": True}  # route() will re-read

        faults.maybe_fail(run["input"], step_id, run["attempt"], rec["invocations"])
        context = {"input": run["input"], "steps": run["context"]["steps"], "human": run["context"].get("human")}
        outcome = pipeline.STEP_FUNCTIONS[step_id](context, self.agent)

        if step_id == FINALIZE:
            next_step, new_status = None, S.COMPLETED
        elif outcome.human:
            next_step, new_status = HUMAN, S.WAITING_FOR_HUMAN
        else:
            next_step, new_status = _next_after(step_id), S.RUNNING
        if new_status is not S.RUNNING:
            assert_transition(S.RUNNING, new_status)
        request = None
        if outcome.human:
            request = {"request_id": request_id_for(run_id, step_id), "question": outcome.human.question,
                       "options": list(outcome.human.options)}
        try:
            self.store.commit_step(run_id, step_id, result=outcome.result, next_step=next_step,
                                   new_status=new_status, effect=outcome.effect, request=request)
        except ConditionFailed:
            return self._reconcile_commit(run_id, step_id)
        return {"step_id": step_id, "reused": False, "status": new_status.value}

    def _reconcile_commit(self, run_id: str, step_id: str) -> dict:
        """Our commit lost. Either another worker already committed it, or the run left RUNNING."""
        rec = self.store.get_step(run_id, step_id)
        if rec and rec["status"] == StepStatus.DONE.value:
            return {"step_id": step_id, "reused": True}  # concurrent winner; its result stands
        run = self._must_get(run_id)
        if run["status"] != S.RUNNING.value:
            return {"halted": True, "status": run["status"]}  # e.g. stopped mid-step; result discarded
        raise TransientError(f"commit of {step_id} conflicted; retry")

    def register_wait(self, run_id: str, request_id: str, task_token: str) -> dict:
        """Called by the state machine's waitForTaskToken task: park the token on the request."""
        try:
            self.store.set_request_token(run_id, request_id, task_token)
        except ConditionFailed:
            raise PermanentError(f"request {request_id} missing for {run_id}") from None
        req = self.store.get_request(run_id, request_id) or {}
        run = self.store.get_run(run_id) or {}
        if req.get("status") == RequestStatus.ANSWERED.value and run.get("status") == S.RUNNING.value:
            # Answer arrived before the token was registered: wake the execution ourselves.
            self.orch.deliver(run_id, request_id, task_token, {"decision": req.get("decision")})
        return {"registered": True}

    def mark_failed(self, run_id: str, step_id: str, error_type: str, message: str) -> dict:
        try:
            self.store.transition(run_id, S.FAILED, sources=[S.RUNNING], event="RUN_FAILED",
                                  set_attrs={"error": {"step": step_id, "type": error_type, "message": message},
                                             "failed_step": step_id},
                                  detail={"step": step_id, "error_type": error_type, "message": message})
        except ConditionFailed:
            return {"ignored": True}  # already stopped/completed: don't clobber it with FAILED
        return {"failed": True}

    # ============================================================== helpers
    def _must_get(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        if run is None:
            raise RunNotFound(f"no run {run_id}")
        return run


def _next_after(step_id: str) -> Optional[str]:
    return {VALIDATE: AGENT, AGENT: FINALIZE}[step_id]
