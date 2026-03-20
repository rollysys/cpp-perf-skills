#!/bin/bash
set -e
cd "$(dirname "$0")"
g++ -O2 -std=c++17 benchmark.cpp -o benchmark -lm
