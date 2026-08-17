#!/usr/bin/env bash
# Runs every test under tests/ against this repo's own bot/ package.
#
# No real API keys needed: each test file sets its own dummy BOT_TOKEN/
# GROQ_API_KEY/ADMIN_IDS and mocks every outbound Groq/AITUNNEL call before
# importing anything from bot/ — see README.md's "Тесты" section.
#
# Exit code is non-zero if any test file fails, so this is safe to use as
# a CI/pre-push gate.

set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PYTHON="${PYTHON:-python3}"

fail_count=0
failed_files=()

for f in tests/run_*.py; do
    echo "=== $f ==="
    if ! "$PYTHON" "$f"; then
        fail_count=$((fail_count + 1))
        failed_files+=("$f")
    fi
    echo
done

if [ "$fail_count" -gt 0 ]; then
    echo "FAILED: $fail_count file(s) — ${failed_files[*]}"
    exit 1
fi

echo "All test files passed."
