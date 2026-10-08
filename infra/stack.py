"""One stack: DynamoDB table, two Lambdas (control + worker), one Standard state machine.

The state machine is a tiny loop:  Route -> {RunStep | WaitForHuman | Done | Halted} -> Route
It holds NO business state: `Route` asks the engine (which reads DynamoDB) what to do next. That is
why start and resume are the same path, and why a stopped run can be resumed in a fresh execution.
"""
from pathlib import Path

from aws_cdk import ArnFormat, CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

SRC = Path(__file__).resolve().parent.parent / "src"
# Only our own transient class is added here; CDK's LambdaInvoke already retries Lambda service errors.
RETRYABLE = ["TransientError"]


class AgentRunnerStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        table = dynamodb.Table(
            self, "RunTable",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,  # assessment stack: teardown must remove data
        )

        # Fixed names break the Lambda <-> state machine dependency cycle (ARN is built, not referenced).
        sm_name = "agent-runner-sm"
        sm_arn = self.format_arn(service="states", resource="stateMachine", resource_name=sm_name,
                                 arn_format=ArnFormat.COLON_RESOURCE_NAME)
        env = {"RUNNER_TABLE": table.table_name, "RUNNER_STATE_MACHINE_ARN": sm_arn}
        code = lambda_.Code.from_asset(str(SRC), exclude=["**/__pycache__", "*.egg-info", "**/*.pyc"])

        def fn(name: str, handler: str, function_name: str | None = None) -> lambda_.Function:
            return lambda_.Function(
                self, name, runtime=lambda_.Runtime.PYTHON_3_12, handler=handler, code=code,
                timeout=Duration.seconds(30), memory_size=256, environment=env, function_name=function_name,
                log_group=logs.LogGroup(self, f"{name}Logs", retention=logs.RetentionDays.ONE_WEEK,
                                        removal_policy=RemovalPolicy.DESTROY),
            )

        worker = fn("Worker", "agent_runner.handlers.worker")
        control = fn("Control", "agent_runner.handlers.control", function_name="agent-runner-control")
        table.grant_read_write_data(worker)
        table.grant_read_write_data(control)

        # Waking a waiting run: SendTaskSuccess (no resource-level permissions exist for it).
        send_success = iam.PolicyStatement(actions=["states:SendTaskSuccess"], resources=["*"])
        worker.add_to_role_policy(send_success)
        control.add_to_role_policy(send_success)
        control.add_to_role_policy(iam.PolicyStatement(actions=["states:StartExecution"], resources=[sm_arn]))
        control.add_to_role_policy(iam.PolicyStatement(
            actions=["states:StopExecution"],
            resources=[sm_arn.replace(":stateMachine:", ":execution:") + ":*"]))

        # ------------------------------------------------------------ state machine
        J = sfn.JsonPath

        def mark_failed(id_: str, step_expr) -> tasks.LambdaInvoke:
            t = tasks.LambdaInvoke(
                self, id_, lambda_function=worker, payload_response_only=True, result_path=J.DISCARD,
                payload=sfn.TaskInput.from_object({"op": "mark_failed", "run_id": J.string_at("$.run_id"),
                                                   "step_id": step_expr, "error": J.object_at("$.error")}))
            t.next(sfn.Fail(self, f"{id_}Fail", error="RunFailed", cause="see run history in DynamoDB"))
            return t

        route = tasks.LambdaInvoke(
            self, "Route", lambda_function=worker, payload_response_only=True, result_path="$.next",
            payload=sfn.TaskInput.from_object({"op": "route", "run_id": J.string_at("$.run_id")}))
        route.add_catch(mark_failed("MarkFailedRoute", "route"), errors=["States.ALL"], result_path="$.error")

        step_failed = mark_failed("MarkFailed", J.string_at("$.next.step_id"))

        run_step = tasks.LambdaInvoke(
            self, "RunStep", lambda_function=worker, payload_response_only=True, result_path=J.DISCARD,
            payload=sfn.TaskInput.from_object({"op": "run_step", "run_id": J.string_at("$.run_id"),
                                               "step_id": J.string_at("$.next.step_id")}))
        run_step.add_retry(errors=RETRYABLE, interval=Duration.seconds(2), max_attempts=3, backoff_rate=2)
        run_step.add_catch(step_failed, errors=["States.ALL"], result_path="$.error")

        # Durable wait: the Lambda only PARKS the task token on the request item and returns.
        # The execution then sleeps (no compute, no polling) until SendTaskSuccess (human answer).
        wait_human = tasks.LambdaInvoke(
            self, "WaitForHuman", lambda_function=worker, result_path=J.DISCARD,
            integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            payload=sfn.TaskInput.from_object({"op": "register_wait", "run_id": J.string_at("$.run_id"),
                                               "request_id": J.string_at("$.next.request_id"),
                                               "task_token": J.task_token}))
        wait_human.add_retry(errors=RETRYABLE, interval=Duration.seconds(2), max_attempts=3, backoff_rate=2)
        wait_human.add_catch(step_failed, errors=["States.ALL"], result_path="$.error")

        choice = (sfn.Choice(self, "NextAction")
                  .when(sfn.Condition.string_equals("$.next.action", "run_step"), run_step)
                  .when(sfn.Condition.string_equals("$.next.action", "wait_human"), wait_human)
                  .when(sfn.Condition.string_equals("$.next.action", "done"), sfn.Succeed(self, "Done"))
                  .otherwise(sfn.Succeed(self, "Halted")))  # stopped / failed: DynamoDB holds the state
        route.next(choice)
        run_step.next(route)
        wait_human.next(route)

        machine = sfn.StateMachine(
            self, "Machine", state_machine_name=sm_name, state_machine_type=sfn.StateMachineType.STANDARD,
            definition_body=sfn.DefinitionBody.from_chainable(route), timeout=Duration.days(365),
            logs=sfn.LogOptions(
                destination=logs.LogGroup(self, "MachineLogs", retention=logs.RetentionDays.ONE_WEEK,
                                          removal_policy=RemovalPolicy.DESTROY),
                level=sfn.LogLevel.ERROR),
        )

        CfnOutput(self, "TableName", value=table.table_name)
        CfnOutput(self, "StateMachineArn", value=machine.state_machine_arn)
        CfnOutput(self, "ControlFunctionName", value=control.function_name)
