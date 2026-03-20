#!/bin/bash
set -e
cd "$(dirname "$0")"
COMPILER="${COMPILER:-g++}"
FLAGS="${FLAGS:--O2 -std=c++17}"
$COMPILER $FLAGS benchmark.cpp -o benchmark -lm
echo "Build OK"
