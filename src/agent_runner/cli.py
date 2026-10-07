"""`runner` CLI.

aws mode   (default): invokes the deployed control Lambda (no business logic in the client).
local mode (RUNNER_MODE=local): runs the same engine in-process against DynamoDB at RUNNER_DDB_ENDPOINT.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from .errors import RunnerError


class AwsClient:
    def __init__(self) -> None:
        import boto3
        self.lam = boto3.client("lambda")
        self.function = _control_function_name()

    def call(self, op: str, **args: Any) -> Any:
        resp = self.lam.invoke(FunctionName=self.function, Payload=json.dumps({"op": op, "args": args}).encode())
        payload = json.loads(resp["Payload"].read())
        if resp.get("FunctionError"):
            raise RunnerError(f"control Lambda crashed: {payload.get('errorMessage', payload)}")
        if not payload["ok"]:
            raise RunnerError(f"{payload['error']['type']}: {payload['error']['message']}")
        return payload["result"]


class LocalClient:
    def __init__(self) -> None:
        from .config import build_engine
        self.engine = build_engine("local")
        self.engine.store.create_table()  # idempotent

    def call(self, op: str, **a: Any) -> Any:
        e = self.engine
        return {
            "start": lambda: e.start(a["input"], a.get("idempotency_key")),
            "status": lambda: e.status(a["run_id"]),
            "pending": lambda: e.pending(a["run_id"]),
            "stop": lambda: e.stop(a["run_id"]),
            "resume": lambda: e.resume(a["run_id"]),
            "answer": lambda: e.answer(a["run_id"], a["request_id"], a["decision"], a.get("comment")),
        }[op]()


def _control_function_name() -> str:
    if os.environ.get("RUNNER_CONTROL_FUNCTION"):
        return os.environ["RUNNER_CONTROL_FUNCTION"]
    outputs = Path(os.environ.get("RUNNER_CDK_OUTPUTS", "cdk-outputs.json"))
    if outputs.exists():
        for stack in json.loads(outputs.read_text()).values():
            if "ControlFunctionName" in stack:
                return stack["ControlFunctionName"]
    return "agent-runner-control"


def format_status(st: dict) -> str:
    lines = [f"Run {st['run_id']}  status={st['status']}  current_step={st['current_step']}  attempt={st['attempt']}"]
    lines.append("  steps:")
    for s in st["steps"]:
        lines.append(f"    {s['step_id']:<16} {s['status']:<8} invocations={s['invocations']}")
    p = st["pending_request"]
    if p:
        lines.append(f"  pending request: {p['request_id']}  {p['question']!r}  options={'|'.join(p['options'])}")
    if st["error"]:
        e = st["error"]
        lines.append(f"  error: step={e['step']} {e['type']}: {e['message']}")
    lines.append(f"  effects (side effects recorded): {len(st['effects'])}")
    lines.append("  history:")
    for h in st["history"]:
        extra = {k: v for k, v in h.items() if k not in ("at", "type")}
        lines.append(f"    {h['at'][11:23]}  {h['type']:<22} {json.dumps(extra) if extra else ''}".rstrip())
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="runner", description="Durable, pausable agent runner")
    ap.add_argument("--mode", choices=["aws", "local"], default=os.environ.get("RUNNER_MODE", "aws"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="start a run; prints the run_id")
    s.add_argument("input", help='JSON, e.g. \'{"customer":"Vijay","amount":5000,"item":"Laptop"}\'')
    s.add_argument("--key", help="idempotency key: starting twice with the same key returns the same run")
    for name, h in (("status", "show state, steps, pending request, history"), ("pending", "show the pending human request"),
                    ("stop", "stop a RUNNING or WAITING run (state preserved)"),
                    ("resume", "resume a STOPPED or FAILED run")):
        p = sub.add_parser(name, help=h)
        p.add_argument("run_id")
        if name == "status":
            p.add_argument("--json", action="store_true")
    a = sub.add_parser("answer", help="submit the human decision for a pending request")
    a.add_argument("run_id")
    a.add_argument("request_id")
    a.add_argument("decision", choices=["approve", "reject"])
    a.add_argument("--comment")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = LocalClient() if args.mode == "local" else AwsClient()
        if args.cmd == "start":
            try:
                payload = json.loads(args.input)
            except ValueError as e:
                raise RunnerError(f"input is not valid JSON: {e}") from None
            out: Any = client.call("start", input=payload, idempotency_key=args.key)
        elif args.cmd == "answer":
            out = client.call("answer", run_id=args.run_id, request_id=args.request_id,
                              decision=args.decision, comment=args.comment)
        else:
            out = client.call(args.cmd, run_id=args.run_id)
        if args.cmd == "status" and not args.json:
            print(format_status(out))
        else:
            print(json.dumps(out, indent=2, default=str))
        return 0
    except RunnerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
