#!/bin/sh
# Start the JobSpy dashboard on Linux / macOS (needs Python 3.10+).
cd "$(dirname "$0")" || exit 1
exec python3 run.py "$@"
