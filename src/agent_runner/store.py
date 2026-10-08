"""All DynamoDB access. Single table:

  PK = RUN#<run_id>   SK = META | STEP#<id> | REQ#<request_id> | EFFECT#<step> | EVT#<ns>#<rand>

State-changing operations are conditional writes or ONE transaction, so concurrent callers can't
produce invalid transitions or duplicate side effects. No read-then-write without a condition.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional, Sequence

import boto3
from botocore.exceptions import ClientError

from .errors import TransientError
from .models import RequestStatus, RunStatus, StepStatus

_THROTTLE_CODES = {
    "ProvisionedThroughputExceededException", "ThrottlingException", "RequestLimitExceeded",
    "InternalServerError", "ServiceUnavailable",
}
_TXN_TRANSIENT_REASONS = {"TransactionConflict", "ThrottlingError", "ProvisionedThroughputExceeded"}


class ConditionFailed(Exception):
    """A conditional write / transaction was rejected (invalid source state, duplicate, stale)."""

    def __init__(self, reasons: Sequence[Optional[str]] = ()):
        super().__init__(f"condition failed: {list(reasons)}")
        self.reasons = list(reasons)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _to_ddb(obj: Any) -> Any:
    """DynamoDB rejects float; round-trip through JSON to get Decimals."""
    return json.loads(json.dumps(obj), parse_float=Decimal)


def _from_ddb(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: _from_ddb(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_from_ddb(v) for v in obj]
    return obj


def pk(run_id: str) -> str:
    return f"RUN#{run_id}"


class Store:
    def __init__(self, table_name: str, endpoint_url: Optional[str] = None, region_name: Optional[str] = None):
        self.table_name = table_name
        kwargs: dict[str, Any] = {}
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        if region_name:
            kwargs["region_name"] = region_name
        self._resource = boto3.resource("dynamodb", **kwargs)
        self.table = self._resource.Table(table_name)
        self.client = self._resource.meta.client

    # ------------------------------------------------------------------ setup (local/tests)
    def create_table(self) -> None:
        try:
            self._resource.create_table(
                TableName=self.table_name,
                KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
                AttributeDefinitions=[
                    {"AttributeName": "PK", "AttributeType": "S"},
                    {"AttributeName": "SK", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            ).wait_until_exists()
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceInUseException":
                raise

    # ------------------------------------------------------------------ low-level helpers
    def _transact(self, items: list[dict]) -> None:
        try:
            self.client.transact_write_items(TransactItems=items)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "TransactionCanceledException":
                reasons = [r.get("Code") for r in e.response.get("CancellationReasons", [])]
                if any(r in _TXN_TRANSIENT_REASONS for r in reasons):
                    raise TransientError(f"transaction conflict: {reasons}") from e
                raise ConditionFailed(reasons) from e
            if code in _THROTTLE_CODES:
                raise TransientError(code) from e
            raise

    @staticmethod
    def _event_item(table: str, run_id: str, type_: str, detail: Optional[Mapping[str, Any]] = None) -> dict:
        return {"Put": {
            "TableName": table,
            "Item": _to_ddb({
                "PK": pk(run_id), "SK": f"EVT#{time.time_ns():020d}#{uuid.uuid4().hex[:6]}",
                "type": type_, "at": utcnow(), "detail": dict(detail or {}),
            }),
        }}

    def _evt(self, run_id: str, type_: str, detail: Optional[Mapping[str, Any]] = None) -> dict:
        return self._event_item(self.table_name, run_id, type_, detail)

    # ------------------------------------------------------------------ reads
    def _get(self, run_id: str, sk: str) -> Optional[dict]:
        try:
            item = self.table.get_item(Key={"PK": pk(run_id), "SK": sk}, ConsistentRead=True).get("Item")
        except ClientError as e:
            if e.response["Error"]["Code"] in _THROTTLE_CODES:
                raise TransientError(e.response["Error"]["Code"]) from e
            raise
        return _from_ddb(item) if item else None

    def get_run(self, run_id: str) -> Optional[dict]:
        return self._get(run_id, "META")

    def get_step(self, run_id: str, step_id: str) -> Optional[dict]:
        return self._get(run_id, f"STEP#{step_id}")

    def get_request(self, run_id: str, request_id: str) -> Optional[dict]:
        return self._get(run_id, f"REQ#{request_id}")

    def get_bundle(self, run_id: str) -> Optional[dict]:
        """Everything about a run in one consistent query."""
        items: list[dict] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "PK = :pk", "ExpressionAttributeValues": {":pk": pk(run_id)},
            "ConsistentRead": True,
        }
        while True:
            resp = self.table.query(**kwargs)
            items.extend(resp["Items"])
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        items = [_from_ddb(i) for i in items]
        run = next((i for i in items if i["SK"] == "META"), None)
        if run is None:
            return None
        pick = lambda prefix: [i for i in items if i["SK"].startswith(prefix)]  # noqa: E731
        return {
            "run": run,
            "steps": {i["SK"][5:]: i for i in pick("STEP#")},
            "requests": {i["SK"][4:]: i for i in pick("REQ#")},
            "effects": pick("EFFECT#"),
            "events": sorted(pick("EVT#"), key=lambda i: i["SK"]),
        }

    # ------------------------------------------------------------------ writes
    def create_run(self, run_id: str, run_input: Mapping[str, Any], first_step: str,
                   idempotency_key: Optional[str] = None) -> bool:
        """Create the run. Returns False if it already exists (duplicate start)."""
        now = utcnow()
        item = {
            "PK": pk(run_id), "SK": "META", "run_id": run_id, "status": RunStatus.CREATED.value,
            "current_step": first_step, "input": dict(run_input), "context": {"steps": {}},
            "attempt": 1, "version": 0, "created_at": now, "updated_at": now,
        }
        if idempotency_key:
            item["idempotency_key"] = idempotency_key
        try:
            self._transact([
                {"Put": {"TableName": self.table_name, "Item": _to_ddb(item),
                         "ConditionExpression": "attribute_not_exists(PK)"}},
                self._evt(run_id, "RUN_CREATED"),
            ])
        except ConditionFailed:
            return False
        return True

    def transition(self, run_id: str, to: RunStatus, *, sources: Iterable[RunStatus], event: str,
                   expect_version: Optional[int] = None, set_attrs: Optional[Mapping[str, Any]] = None,
                   remove_attrs: Iterable[str] = (), detail: Optional[Mapping[str, Any]] = None) -> None:
        """Conditional status change (+ event) in one transaction. Raises ConditionFailed."""
        names = {"#status": "status", "#updated_at": "updated_at", "#version": "version"}
        values: dict[str, Any] = {":to": to.value, ":now": utcnow(), ":one": 1}
        sets = ["#status = :to", "#updated_at = :now", "#version = #version + :one"]
        for i, (k, v) in enumerate((set_attrs or {}).items()):
            names[f"#a{i}"], values[f":a{i}"] = k, v
            sets.append(f"#a{i} = :a{i}")
        removes = []
        for i, k in enumerate(remove_attrs):
            names[f"#r{i}"] = k
            removes.append(f"#r{i}")
        src_ph = []
        for i, s in enumerate(sources):
            values[f":s{i}"] = s.value
            src_ph.append(f":s{i}")
        cond = f"attribute_exists(PK) AND #status IN ({', '.join(src_ph)})"
        if expect_version is not None:
            values[":ev"] = expect_version
            cond += " AND #version = :ev"
        expr = "SET " + ", ".join(sets) + (" REMOVE " + ", ".join(removes) if removes else "")
        self._transact([
            {"Update": {"TableName": self.table_name, "Key": {"PK": pk(run_id), "SK": "META"},
                        "UpdateExpression": expr, "ConditionExpression": cond,
                        "ExpressionAttributeNames": names, "ExpressionAttributeValues": _to_ddb(values)}},
            self._evt(run_id, event, {"to": to.value, **(detail or {})}),
        ])

    def begin_step(self, run_id: str, step_id: str) -> dict:
        """Record an invocation of the step; returns the persisted step item (so callers can see DONE)."""
        now = utcnow()
        try:
            resp = self.table.update_item(
                Key={"PK": pk(run_id), "SK": f"STEP#{step_id}"},
                UpdateExpression=("SET #status = if_not_exists(#status, :started), #run_id = :rid, "
                                  "#step_id = :sid, #first = if_not_exists(#first, :now) "
                                  "ADD #inv :one"),
                ExpressionAttributeNames={"#status": "status", "#run_id": "run_id", "#step_id": "step_id",
                                          "#first": "first_started_at", "#inv": "invocations"},
                ExpressionAttributeValues={":started": StepStatus.STARTED.value, ":rid": run_id,
                                           ":sid": step_id, ":now": now, ":one": 1},
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] in _THROTTLE_CODES:
                raise TransientError(e.response["Error"]["Code"]) from e
            raise
        return _from_ddb(resp["Attributes"])

    def commit_step(self, run_id: str, step_id: str, *, result: Mapping[str, Any], next_step: Optional[str],
                    new_status: RunStatus, effect: Optional[Mapping[str, Any]] = None,
                    request: Optional[Mapping[str, Any]] = None) -> None:
        """ONE transaction: step DONE + pointer advance + status + side effect + human request + events.

        Conditions: step not already DONE, run RUNNING, and pointer still at this step.
        Raises ConditionFailed if any fails (caller reconciles).
        """
        now = utcnow()
        t = self.table_name
        meta_names = {"#status": "status", "#cur": "current_step", "#ctx": "context", "#steps": "steps",
                      "#sid": step_id, "#upd": "updated_at", "#ver": "version"}
        meta_values: dict[str, Any] = {":run": RunStatus.RUNNING.value, ":ns": new_status.value,
                                       ":step": step_id, ":next": next_step, ":res": dict(result),
                                       ":now": now, ":one": 1}
        sets = ["#status = :ns", "#cur = :next", "#ctx.#steps.#sid = :res", "#upd = :now", "#ver = #ver + :one"]
        if request:
            meta_names["#prid"] = "pending_request_id"
            meta_values[":prid"] = request["request_id"]
            sets.append("#prid = :prid")
        items: list[dict] = [
            {"Update": {
                "TableName": t, "Key": {"PK": pk(run_id), "SK": f"STEP#{step_id}"},
                "UpdateExpression": "SET #status = :done, #result = :res, #fin = :now",
                "ConditionExpression": "attribute_exists(PK) AND #status <> :done",
                "ExpressionAttributeNames": {"#status": "status", "#result": "result", "#fin": "finished_at"},
                "ExpressionAttributeValues": _to_ddb(
                    {":done": StepStatus.DONE.value, ":res": dict(result), ":now": now}),
            }},
            {"Update": {
                "TableName": t, "Key": {"PK": pk(run_id), "SK": "META"},
                "UpdateExpression": "SET " + ", ".join(sets),
                "ConditionExpression": "#status = :run AND #cur = :step",
                "ExpressionAttributeNames": meta_names, "ExpressionAttributeValues": _to_ddb(meta_values),
            }},
            self._evt(run_id, "STEP_COMPLETED", {"step": step_id}),
        ]
        if effect:
            items.append({"Put": {
                "TableName": t,
                "Item": _to_ddb({"PK": pk(run_id), "SK": f"EFFECT#{step_id}", "step_id": step_id,
                                 "created_at": now, **dict(effect)}),
                "ConditionExpression": "attribute_not_exists(PK)",
            }})
        if request:
            items.append({"Put": {
                "TableName": t,
                "Item": _to_ddb({"PK": pk(run_id), "SK": f"REQ#{request['request_id']}", "run_id": run_id,
                                 "step_id": step_id, "status": RequestStatus.PENDING.value,
                                 "created_at": now, **dict(request)}),
                "ConditionExpression": "attribute_not_exists(PK)",
            }})
            items.append(self._evt(run_id, "HUMAN_INPUT_REQUESTED", {"request_id": request["request_id"]}))
        if new_status is RunStatus.COMPLETED:
            items.append(self._evt(run_id, "RUN_COMPLETED"))
        self._transact(items)

    def answer_request(self, run_id: str, request_id: str, decision: str, comment: Optional[str],
                       next_step: str, human_step: str) -> None:
        """PENDING->ANSWERED + WAITING->RUNNING + pointer advance in ONE transaction.

        Raises ConditionFailed if the request isn't PENDING or the run isn't waiting on it.
        """
        now = utcnow()
        t = self.table_name
        human = {"request_id": request_id, "decision": decision, "comment": comment, "answered_at": now}
        self._transact([
            {"Update": {
                "TableName": t, "Key": {"PK": pk(run_id), "SK": f"REQ#{request_id}"},
                "UpdateExpression": "SET #status = :ans, #dec = :dec, #com = :com, #at = :now",
                "ConditionExpression": "attribute_exists(PK) AND #status = :pend",
                "ExpressionAttributeNames": {"#status": "status", "#dec": "decision", "#com": "comment",
                                             "#at": "answered_at"},
                "ExpressionAttributeValues": _to_ddb({":ans": RequestStatus.ANSWERED.value,
                                                      ":pend": RequestStatus.PENDING.value,
                                                      ":dec": decision, ":com": comment, ":now": now}),
            }},
            {"Update": {
                "TableName": t, "Key": {"PK": pk(run_id), "SK": "META"},
                "UpdateExpression": ("SET #status = :run, #cur = :next, #ctx.#human = :human, #upd = :now, "
                                     "#ver = #ver + :one REMOVE #prid"),
                "ConditionExpression": "#status = :wait AND #prid = :rid",
                "ExpressionAttributeNames": {"#status": "status", "#cur": "current_step", "#ctx": "context",
                                             "#human": "human", "#upd": "updated_at", "#ver": "version",
                                             "#prid": "pending_request_id"},
                "ExpressionAttributeValues": _to_ddb({":run": RunStatus.RUNNING.value,
                                                      ":wait": RunStatus.WAITING_FOR_HUMAN.value,
                                                      ":next": next_step, ":human": human, ":now": now,
                                                      ":one": 1, ":rid": request_id}),
            }},
            {"Put": {
                "TableName": t,
                "Item": _to_ddb({"PK": pk(run_id), "SK": f"STEP#{human_step}", "run_id": run_id,
                                 "step_id": human_step, "status": StepStatus.DONE.value, "invocations": 0,
                                 "result": {"decision": decision, "comment": comment}, "finished_at": now}),
                "ConditionExpression": "attribute_not_exists(PK)",
            }},
            self._evt(run_id, "HUMAN_INPUT_RECEIVED", {"request_id": request_id, "decision": decision}),
        ])

    def set_request_token(self, run_id: str, request_id: str, token: str) -> None:
        try:
            self.table.update_item(
                Key={"PK": pk(run_id), "SK": f"REQ#{request_id}"},
                UpdateExpression="SET #tok = :tok, #at = :now",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeNames={"#tok": "task_token", "#at": "token_registered_at"},
                ExpressionAttributeValues={":tok": token, ":now": utcnow()},
            )
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "ConditionalCheckFailedException":
                raise ConditionFailed(["request missing"]) from e
            if code in _THROTTLE_CODES:
                raise TransientError(code) from e
            raise
