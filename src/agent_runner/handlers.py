"""Lambda entrypoints.

control(event): {"op": "start|status|pending|stop|resume|answer", "args": {...}}  (used by the CLI)
worker(event):  {"op": "route|run_step|register_wait|mark_failed", ...}            (used by the state machine)

Worker errors are raised as-is so Step Functions can match on the exception class name.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .engine import Engine
from .errors import RunnerError, RunNotRunning

log = logging.getLogger()
log.setLevel(logging.INFO)

_engine: Optional[Engine] = None


def set_engine(engine: Optional[Engine]) -> None:  # tests
    global _engine
    _engine = engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        from .config import build_engine
        _engine = build_engine("aws")
    return _engine


def control(event: dict, context: Any = None) -> dict:
    e, op, a = get_engine(), event.get("op"), event.get("args", {})
    try:
        if op == "start":
            result = e.start(a["input"], a.get("idempotency_key"))
        elif op == "status":
            result = e.status(a["run_id"])
        elif op == "pending":
            result = e.pending(a["run_id"])
        elif op == "stop":
            result = e.stop(a["run_id"])
        elif op == "resume":
            result = e.resume(a["run_id"])
        elif op == "answer":
            result = e.answer(a["run_id"], a["request_id"], a["decision"], a.get("comment"))
        else:
            return {"ok": False, "error": {"type": "BadInput", "message": f"unknown op {op!r}"}}
        return {"ok": True, "result": result}
    except RunnerError as err:
        return {"ok": False, "error": {"type": type(err).__name__, "message": str(err)}}


def worker(event: dict, context: Any = None) -> dict:
    e, op = get_engine(), event.get("op")
    log.info("worker op=%s run=%s", op, event.get("run_id"))
    if op == "route":
        return e.route(event["run_id"])
    if op == "run_step":
        try:
            return e.run_step(event["run_id"], event["step_id"])
        except RunNotRunning:
            return {"halted": True}
    if op == "register_wait":
        return e.register_wait(event["run_id"], event["request_id"], event["task_token"])
    if op == "mark_failed":
        err = event.get("error") or {}
        return e.mark_failed(event["run_id"], event.get("step_id") or "unknown",
                             err.get("Error", "Unknown"), _cause_message(err.get("Cause")))
    raise ValueError(f"unknown worker op {op!r}")


def _cause_message(cause: Any) -> str:
    """Step Functions gives Lambda failures as a JSON string with errorMessage."""
    if isinstance(cause, str):
        try:
            return str(json.loads(cause).get("errorMessage", cause))
        except (ValueError, AttributeError):
            return cause
    return str(cause)
