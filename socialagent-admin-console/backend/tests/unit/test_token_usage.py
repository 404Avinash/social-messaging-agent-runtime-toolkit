# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Token usage reports counts and the full model ID, and no cost.

The endpoint priced tokens from a table of 3.x-era prices matched by substring,
with a Sonnet fallback for unknown models, and cut the model name at the last
colon, so a ``...-v1:0`` ID showed as "0". No console page reads the cost.
"""

import json
from unittest.mock import MagicMock, patch


def test_token_usage_has_no_cost_and_keeps_the_model_id():
    from routes import stacks

    model = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    logs_client = MagicMock()
    logs_client.filter_log_events.return_value = {"events": [{"message": json.dumps({
        "modelId": model,
        "input": {"inputTokenCount": 100},
        "output": {"outputTokenCount": 10},
    })}]}
    with (
        patch.object(stacks, "_get_registry", return_value={"outputs": {}, "region": "us-east-1"}),
        patch.object(stacks, "_lambda_metrics", return_value={}),
        patch.object(stacks.boto3, "client", return_value=logs_client),
    ):
        resp = stacks.handle_get_metrics({"id": "dev-bot"}, {})

    usage = json.loads(resp["body"])["token_usage"]
    assert usage["model"] == model
    assert usage["input_tokens_24h"] == 100
    assert usage["output_tokens_24h"] == 10
    assert "estimated_cost_24h_usd" not in usage
