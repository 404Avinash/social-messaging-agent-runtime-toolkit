#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
E2E smoke tests against a deployed SMART stack.

Reads connection info from socialagent.<env>.yaml and invokes the AgentCore Runtime
directly. Tests the full agent loop: prompt -> LLM -> tools -> response.

Usage:
    cd projects/<your-bot>
    AWS_PROFILE=<profile> python ../../scripts/e2e_test.py
    AWS_PROFILE=<profile> python ../../scripts/e2e_test.py --env prod
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import boto3
import yaml

PHONE = "+5215500000000"
DIGITS = "5215500000000"
PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"


def load_config(env: str) -> dict:
    path = Path.cwd() / f"socialagent.{env}.yaml"
    if not path.exists():
        print(f"{FAIL} Config not found: {path}")
        print(f"  Run: socialagent config init --env {env} && socialagent config sync --env {env}")
        sys.exit(1)
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def invoke(client, runtime_arn: str, prompt: str, session_id: str, memory_id: str = "__socialagent_dev__") -> str:
    ts = int(time.time())
    payload = {
        "prompt": prompt,
        "phone": PHONE,
        "socialagent": {
            "version": 1,
            "memory_id": memory_id,
            "session_id": f"whatsapp-session-conversation-e2e-{session_id}",
            "actor_id": DIGITS,
            "message_id": f"e2e-{ts}",
            "message_type": "text",
            "region": "us-east-1",
        },
    }
    # runtimeSessionId must be >= 33 chars
    runtime_session = f"e2e-smoke-test-session-{session_id}-{ts}"
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=runtime_session,
        payload=json.dumps(payload),
    )
    body = resp["response"]
    raw = body.read() if hasattr(body, "read") else body
    result = json.loads(raw)
    return result.get("result", str(result))


def test_greeting(client, arn, memory_id) -> bool:
    text = invoke(client, arn, "Hi, what can you do?", f"greeting-{int(time.time())}", memory_id)
    ok = len(text) > 20
    print(f"  {PASS if ok else FAIL} Greeting response ({len(text)} chars)")
    if not ok:
        print(f"    Response: {text[:200]}")
    return ok


def test_whatsapp_formatting(client, arn, memory_id) -> bool:
    text = invoke(client, arn, "List 3 things you can help with", f"fmt-{int(time.time())}", memory_id)
    has_bold = "*" in text
    has_emoji = any(ord(c) > 0x1F600 for c in text)
    ok = has_bold or has_emoji
    print(f"  {PASS if ok else FAIL} WhatsApp formatting (bold={has_bold}, emoji={has_emoji})")
    if not ok:
        print(f"    Response: {text[:200]}")
    return ok


def test_remember_recall(client, arn, memory_id) -> bool:
    session = f"state-{int(time.time())}"

    # Remember
    text = invoke(client, arn, "Remember that my favorite color is blue", session, memory_id)
    stored = any(w in text.lower() for w in ["stored", "remember", "noted", "got it", "blue", "save"])
    print(f"  {PASS if stored else FAIL} Remember tool (response mentions storage)")

    if not stored:
        print(f"    Response: {text[:200]}")
        return False

    # Recall
    time.sleep(2)
    text = invoke(client, arn, "What is my favorite color?", session, memory_id)
    recalled = "blue" in text.lower()
    print(f"  {PASS if recalled else FAIL} Recall tool (response contains 'blue')")
    if not recalled:
        print(f"    Response: {text[:200]}")
    return stored and recalled


def test_scheduling(client, arn, memory_id) -> bool:
    text = invoke(client, arn, "Remind me in 5 minutes to drink water", f"sched-{int(time.time())}", memory_id)
    ok = any(w in text.lower() for w in ["scheduled", "remind", "minutes", "water", "set", "alarm"])
    print(f"  {PASS if ok else FAIL} Schedule reminder")
    if not ok:
        print(f"    Response: {text[:200]}")
    return ok


def main():
    parser = argparse.ArgumentParser(description="SMART E2E smoke tests")
    parser.add_argument("--env", default="dev", help="Environment (reads socialagent.<env>.yaml)")
    args = parser.parse_args()

    config = load_config(args.env)
    arn = config["outputs"]["runtime_arn"]
    memory_id = config["outputs"].get("memory_id", "__socialagent_dev__")
    project = config.get("project", "?")
    env = config.get("environment", args.env)

    print(f"\nSMART E2E Tests — {project}-{env}")
    print(f"Runtime: {arn}")
    print(f"Memory:  {memory_id}\n")

    client = boto3.client("bedrock-agentcore", region_name=config.get("region", "us-east-1"))

    tests = [
        ("Agent responds to greeting", test_greeting),
        ("WhatsApp formatting", test_whatsapp_formatting),
        ("Remember + Recall", test_remember_recall),
        ("Scheduling", test_scheduling),
    ]

    results = []
    for name, fn in tests:
        print(f"\n[{name}]")
        try:
            results.append(fn(client, arn, memory_id))
        except Exception as e:
            print(f"  {FAIL} Exception: {e}")
            results.append(False)

    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 40}")
    print(f"Results: {passed}/{total} passed")

    if passed == total:
        print(f"\n{PASS} All tests passed!")
    else:
        print(f"\n{FAIL} {total - passed} test(s) failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
