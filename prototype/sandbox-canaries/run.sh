#!/usr/bin/env bash
# PROTOTYPE: check that hack/run-migration-in-sandbox.py tells sandbox-compatible migrations from
# incompatible ones. Every script in scripts/ is run on pipeline.yaml both in the sandbox and
# directly on the host; the outcome must match the script name prefix:
#
#   pass-          succeeds in the sandbox (and on the host)
#   sandbox-fail-  succeeds on the host, fails in the sandbox
#   fail-          fails in both
#
# Usage: run.sh [SCRIPT...]   (default: all scripts)
# Environment: PMT_PYTHON (default python3), PMT_RUNNER_IMAGE, and crun + crane in PATH.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
: "${PMT_PYTHON:=python3}"

# Stands for tokens in the Renovate environment; must never reach the sandbox
export CANARY_SECRET=canary-secret-value

if [ $# -eq 0 ]; then
    set -- "$HERE"/scripts/*.sh
fi

work=$(mktemp -d --suffix=-sandbox-canaries)
trap 'rm -rf "$work"' EXIT

failures=0
printf '%-36s %-14s %-14s %-8s %-8s %s\n' SCRIPT EXPECTED ACTUAL SANDBOX HOST RESULT
for script in "$@"; do
    name=$(basename "$script" .sh)
    case "$name" in
        pass-*) expected=pass ;;
        sandbox-fail-*) expected=sandbox-fail ;;
        fail-*) expected=fail ;;
        *) echo "unknown prefix: $name" >&2; exit 2 ;;
    esac

    mkdir -p "$work/$name/sandbox" "$work/$name/host"
    cp "$HERE/pipeline.yaml" "$work/$name/sandbox/pipeline.yaml"
    cp "$HERE/pipeline.yaml" "$work/$name/host/pipeline.yaml"

    sandbox_status=0
    "$PMT_PYTHON" "$REPO_ROOT/hack/run-migration-in-sandbox.py" "$script" \
        "$work/$name/sandbox/pipeline.yaml" >"$work/$name/sandbox.log" 2>&1 || sandbox_status=$?
    if [ "$sandbox_status" -eq 125 ]; then
        echo "error: the sandbox cannot be used:" >&2
        cat "$work/$name/sandbox.log" >&2
        exit 1
    fi
    host_status=0
    (cd "$work/$name/host" && bash "$script" pipeline.yaml) >"$work/$name/host.log" 2>&1 \
        || host_status=$?

    if [ "$sandbox_status" -eq 0 ]; then
        actual=pass
    elif [ "$host_status" -eq 0 ]; then
        actual=sandbox-fail
    else
        actual=fail
    fi

    result=ok
    if [ "$actual" != "$expected" ]; then
        result=MISMATCH
    elif [ "$actual" = pass ] && cmp -s "$HERE/pipeline.yaml" "$work/$name/sandbox/pipeline.yaml"; then
        result="MISMATCH (pipeline unchanged)"
    elif grep -q "$CANARY_SECRET" "$work/$name/sandbox/pipeline.yaml" "$work/$name/sandbox.log"; then
        result="MISMATCH (secret leaked into the sandbox)"
    fi
    printf '%-36s %-14s %-14s %-8s %-8s %s\n' \
        "$name" "$expected" "$actual" "$sandbox_status" "$host_status" "$result"

    if [ "$result" != ok ] || [ -n "${VERBOSE:-}" ]; then
        [ "$result" != ok ] && failures=$((failures + 1))
        echo "--- sandbox output"; sed 's/^/    /' "$work/$name/sandbox.log"
        echo "--- host output"; sed 's/^/    /' "$work/$name/host.log"
        if [ "$actual" = pass ]; then
            echo "--- sandbox result diff"
            diff -u "$HERE/pipeline.yaml" "$work/$name/sandbox/pipeline.yaml" | sed 's/^/    /'
        fi
    fi
done

echo
if [ "$failures" -ne 0 ]; then
    echo "$failures canary script(s) did not behave as expected"
    exit 1
fi
echo "All $# canary scripts behaved as expected"
