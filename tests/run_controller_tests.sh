#!/bin/bash
set -euo pipefail
python3 -m unittest discover -s tests/controller -v
