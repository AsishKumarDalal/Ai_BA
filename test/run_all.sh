#!/usr/bin/env bash
# Run every end-to-end script against the live endpoint.
#
#   bash test/run_all.sh
#
# Each script checks the system against the *database*, not against a fixed
# expected string: a model answer is only right if the number in it is in the
# database. Set TSQL_TEST_OFFLINE=1 to skip the parts that need the endpoint.
set -u

cd "$(dirname "$0")/.."
PY=.venv/bin/python
LOG=$(mktemp -d)
FAILED=()

for script in test/test_0*.py; do
    name=$(basename "$script")
    echo ""
    echo "════════════════════════════════════════════════════════════════"
    echo "  $name"
    echo "════════════════════════════════════════════════════════════════"
    "$PY" "$script" > "$LOG/$name.log" 2>&1
    status=$?
    grep -v "RuntimeWarning\|ResourceWarning\|_type_convert\|tracemalloc" "$LOG/$name.log"
    if [ $status -ne 0 ]; then
        FAILED+=("$name")
    fi
done

echo ""
echo "════════════════════════════════════════════════════════════════"
if [ ${#FAILED[@]} -eq 0 ]; then
    echo "  all scripts passed"
    rm -rf "$LOG"
    exit 0
fi
echo "  FAILED: ${FAILED[*]}"
echo "  logs in $LOG"
exit 1
