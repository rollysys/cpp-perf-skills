#!/bin/bash
set -e
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$CASE_DIR/expected"
bash build.sh > /dev/null 2>&1
OUTPUT=$(bash run.sh 2>/dev/null)

# Validate JSON structure
echo "$OUTPUT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert 'function' in d, 'missing function field'
assert 'stats' in d, 'missing stats field'
assert 'min' in d['stats'], 'missing stats.min'
assert 'median' in d['stats'], 'missing stats.median'
assert d['stats']['median'] > 0, 'median must be positive'
" || { echo "FAIL: $(basename $CASE_DIR) — invalid JSON output"; exit 1; }

echo "PASS: $(basename $CASE_DIR)"
