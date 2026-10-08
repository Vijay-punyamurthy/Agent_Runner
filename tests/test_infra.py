import json
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("aws_cdk")
if shutil.which("node") is None:  # jsii needs node
    pytest.skip("node not available for CDK synth", allow_module_level=True)

import aws_cdk as cdk  # noqa: E402
from aws_cdk.assertions import Template  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "infra"))
from stack import AgentRunnerStack  # noqa: E402


@pytest.fixture(scope="module")
def template():
    app = cdk.App()
    return Template.from_stack(AgentRunnerStack(app, "T", stack_name="agent-runner"))


def definition(template) -> dict:
    sm = next(iter(template.find_resources("AWS::StepFunctions::StateMachine").values()))
    raw = sm["Properties"]["DefinitionString"]
    parts = raw["Fn::Join"][1] if isinstance(raw, dict) else [raw]
    # intrinsic refs (lambda ARNs) become placeholders so the ASL can be parsed
    return json.loads("".join(p if isinstance(p, str) else "ARN" for p in parts))


def test_table_is_on_demand_with_pk_sk_and_destroyable(template):
    template.has_resource_properties("AWS::DynamoDB::Table", {
        "BillingMode": "PAY_PER_REQUEST",
        "KeySchema": [{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
    })
    template.has_resource("AWS::DynamoDB::Table", {"DeletionPolicy": "Delete"})


def test_two_python_lambdas_without_vpc_or_secrets(template):
    fns = template.find_resources("AWS::Lambda::Function")
    assert len(fns) == 2
    handlers = {f["Properties"]["Handler"] for f in fns.values()}
    assert handlers == {"agent_runner.handlers.worker", "agent_runner.handlers.control"}
    for f in fns.values():
        assert f["Properties"]["Runtime"] == "python3.12"
        assert "VpcConfig" not in f["Properties"]


def test_state_machine_is_standard_and_waits_durably_with_task_token(template):
    template.has_resource_properties("AWS::StepFunctions::StateMachine", {"StateMachineType": "STANDARD"})
    d = definition(template)
    wait = d["States"]["WaitForHuman"]
    assert wait["Resource"].endswith(".waitForTaskToken")
    assert wait["Parameters"]["Payload"]["task_token.$"] == "$$.Task.Token"
    assert "HeartbeatSeconds" not in wait and "TimeoutSeconds" not in wait  # may wait for days
    assert d["StartAt"] == "Route"


def test_retries_only_for_transient_and_everything_else_is_caught_to_mark_failed(template):
    states = definition(template)["States"]
    rules = states["RunStep"]["Retry"]
    assert not any("PermanentError" in r["ErrorEquals"] or "States.ALL" in r["ErrorEquals"] for r in rules)
    mine = next(r for r in rules if "TransientError" in r["ErrorEquals"])
    assert mine["MaxAttempts"] == 3 and mine["BackoffRate"] == 2
    assert states["RunStep"]["Catch"][0]["ErrorEquals"] == ["States.ALL"]
    assert states["RunStep"]["Catch"][0]["Next"] == "MarkFailed"


def test_loop_returns_to_route_after_every_action(template):
    states = definition(template)["States"]
    assert states["RunStep"]["Next"] == "Route" and states["WaitForHuman"]["Next"] == "Route"
    assert states["Route"]["Next"] == "NextAction"


def test_iam_is_scoped(template):
    doc = json.dumps(template.find_resources("AWS::IAM::Policy"))
    assert "states:StartExecution" in doc and "states:StopExecution" in doc and "states:SendTaskSuccess" in doc
    assert "dynamodb:PutItem" in doc
