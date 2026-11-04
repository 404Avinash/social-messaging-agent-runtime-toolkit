# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd -m -u 1000 bedrock_agentcore

COPY agent.py .
COPY socialagent_runtime ./socialagent_runtime
COPY src ./src

RUN chown -R bedrock_agentcore:bedrock_agentcore /app

USER bedrock_agentcore
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/ping', timeout=2)" || exit 1

CMD ["opentelemetry-instrument", "python", "agent.py"]
