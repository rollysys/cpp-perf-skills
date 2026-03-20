#!/bin/bash
set -e
PASS=0; FAIL=0
for case_dir in tests/goldens/case_*/; do
    if bash "$case_dir/validate.sh" 2>/dev/null; then
        PASS=$((PASS + 1))
    else
        echo "FAIL: $case_dir"
        FAIL=$((FAIL + 1))
    fi
done
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
