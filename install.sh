#!/usr/bin/env bash
set -e

python3 -m venv .venv

if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" ]]; then
    source .venv/Scripts/activate
else
    source .venv/bin/activate
fi

# PIP_USER=false overrides any global pip config that sets user=true,
# which would otherwise fail inside a virtual environment.
PIP_USER=false pip install -r requirements.txt
