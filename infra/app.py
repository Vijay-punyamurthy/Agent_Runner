import os

import aws_cdk as cdk
from stack import AgentRunnerStack

app = cdk.App()
account, region = os.environ.get("CDK_DEFAULT_ACCOUNT"), os.environ.get("CDK_DEFAULT_REGION")
AgentRunnerStack(
    app, "AgentRunnerStack", stack_name="agent-runner",
    env=cdk.Environment(account=account, region=region) if account and region else None,
)
app.synth()
