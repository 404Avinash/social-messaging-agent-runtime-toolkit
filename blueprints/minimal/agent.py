# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Minimal echo agent — deployed to AgentCore Runtime via Terraform."""

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from socialagent_runtime import create_memory_client

app = BedrockAgentCoreApp()


@app.entrypoint
async def invoke(payload: dict, context: object = None) -> dict:
    """Handle an inbound WhatsApp message."""
    mem = create_memory_client(payload)
    response = f"Echo: {payload['prompt']}"
    mem.log_user_message(payload["prompt"])
    mem.log_assistant_message(response)
    return {"result": response}


app.run()
