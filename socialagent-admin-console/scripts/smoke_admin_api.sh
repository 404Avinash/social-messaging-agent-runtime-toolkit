#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Smoke test for admin API — hits core read endpoints with a valid JWT.
# Usage:
#   API_URL=https://... ADMIN_TOKEN=eyJ... bash smoke_admin_api.sh
set -euo pipefail

: "${API_URL:?API_URL env var required}"
: "${ADMIN_TOKEN:?ADMIN_TOKEN env var required (Cognito access token)}"

endpoints=(
  "GET /api/stacks"
  "GET /api/settings"
  "GET /api/schedules"
  "GET /api/users"
  "GET /api/phone-numbers"
  "GET /api/templates/health"
  "GET /api/state"
)

fail=0
for ep in "${endpoints[@]}"; do
  method="${ep%% *}"
  path="${ep##* }"
  code=$(curl -s -o /dev/null -w "%{http_code}" \
            -X "$method" \
            -H "Authorization: Bearer $ADMIN_TOKEN" \
            "$API_URL$path")
  printf "%-4s %-40s %s\n" "$method" "$path" "$code"
  if [[ "$code" != "200" ]]; then
    fail=1
  fi
done

if (( fail == 1 )); then
  echo "SMOKE TEST FAILED"
  exit 1
fi
echo "SMOKE TEST PASSED"
