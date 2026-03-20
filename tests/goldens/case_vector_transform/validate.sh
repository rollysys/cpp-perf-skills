#!/bin/bash
set -e
cd "$(dirname "$0")/expected"
bash build.sh
bash run.sh > /dev/null 2>&1
echo "PASS: $(basename $(dirname $(pwd)))"
