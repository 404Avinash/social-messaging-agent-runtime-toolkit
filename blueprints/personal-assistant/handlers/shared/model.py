# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The Bedrock model this project calls.

Set MODEL_ID under `agent.environment` in socialagent.<env>.yaml to use another
model; Terraform passes it to the agent and to the webhook Lambda. See
docs/configuration.md in the SMART repo. The default is the US geographic
inference profile, which keeps inference in US Regions; eu., jp., au. and the
other geographies do the same for theirs, and global. may route to any
commercial Region. See docs/data-flows.md for what that means for content.
"""

import os

DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-5-5"

# `or`, not a get() default: an env var that is present but empty means unset.
MODEL_ID = os.environ.get("MODEL_ID") or DEFAULT_MODEL_ID
