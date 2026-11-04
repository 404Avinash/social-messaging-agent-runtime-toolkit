# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
SMART Admin Console — Backend Lambda handler (entry point).

Thin entry point: imports the route modules (which register their endpoints
on the shared Powertools resolver via import side-effect) and delegates every
request to it. All business logic lives in routes/<resource>.py; shared
helpers (registry access, boto3 clients, HTTP responses) live in shared/.
"""
from __future__ import annotations

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.logging import correlation_paths

logger = Logger(service="admin-console-backend")
tracer = Tracer(service="admin-console-backend")

# Register all route decorators by importing the route modules. Each module
# imports `app` from shared.resolver and registers its @app.get/put/post/delete
# handlers as an import side-effect.
from routes import (  # noqa: E402, F401
    conversations,
    events,
    logs,
    ops,
    phone_numbers,
    schedules,
    settings,
    stacks,
    state,
    templates,
    users,
)
from shared.resolver import app  # noqa: E402


@tracer.capture_lambda_handler
@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_HTTP)
def handler(event, context):
    """API Gateway Lambda entry point.

    Delegates to Powertools APIGatewayHttpResolver, which dispatches to the
    appropriate route handler registered under routes/<resource>.py.
    """
    return app.resolve(event, context)
