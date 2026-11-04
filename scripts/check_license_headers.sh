#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Verify every covered source file carries the Apache-2.0 SPDX header.
#
# The lists below are the normative definition of "covered"; CONTRIBUTING.md's
# Licensing section restates them for humans. Adding a file type means editing
# both in the same commit. Only tracked files are checked, so build output and
# gitignored local config are out of scope by construction. A header may sit
# under a shebang or a yaml-language-server modeline, hence the 8-line scan.

set -euo pipefail

EXTENSIONS=(py tf ts tsx sh yml yaml toml example)
BASENAMES=(Dockerfile Makefile)
EXPECTED="SPDX-License-Identifier: Apache-2.0"

cd "$(git rev-parse --show-toplevel)"

pattern=""
for ext in "${EXTENSIONS[@]}"; do pattern+="${pattern:+|}\\.${ext}\$"; done
for base in "${BASENAMES[@]}"; do pattern+="|(^|/)${base}\$"; done

bad=()
checked=0
while IFS= read -r file; do
  [ -f "$file" ] || continue
  checked=$((checked + 1))
  head -n 8 "$file" | grep -qF "$EXPECTED" || bad+=("$file")
done < <(git ls-files | grep -E "$pattern")

if [ ${#bad[@]} -gt 0 ]; then
  echo "${#bad[@]} file(s) lack the Apache-2.0 SPDX header (see CONTRIBUTING.md > Licensing):" >&2
  printf '  %s\n' "${bad[@]}" >&2
  exit 1
fi

echo "License headers OK ($checked files checked)."
