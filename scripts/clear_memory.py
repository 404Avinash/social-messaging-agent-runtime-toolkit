#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Clear all events from AgentCore Memory for all actors."""
import sys
import boto3
from concurrent.futures import ThreadPoolExecutor


def main():
    memory_id = sys.argv[1] if len(sys.argv) > 1 else None
    if not memory_id:
        print("Usage: python3 clear_memory.py <memory_id>")
        sys.exit(1)

    ac = boto3.client("bedrock-agentcore", region_name="us-east-1")
    actors = ac.list_actors(memoryId=memory_id, maxResults=50).get("actorSummaries", [])
    print(f"Found {len(actors)} actors")

    def clear_actor(actor_id):
        count = 0
        sessions = ac.list_sessions(memoryId=memory_id, actorId=actor_id, maxResults=50)
        for s in sessions.get("sessionSummaries", []):
            sid = s["sessionId"]
            while True:
                events = ac.list_events(memoryId=memory_id, actorId=actor_id, sessionId=sid, maxResults=100)
                batch = events.get("events", [])
                if not batch:
                    break
                for ev in batch:
                    try:
                        ac.delete_event(memoryId=memory_id, actorId=actor_id, sessionId=sid, eventId=ev["eventId"])
                        count += 1
                    except Exception:
                        pass
        return count

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda a: clear_actor(a["actorId"]), actors))
        deleted = sum(results)

    print(f"✅ Deleted {deleted} events across {len(actors)} actors")


if __name__ == "__main__":
    main()
