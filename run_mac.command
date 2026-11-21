#!/bin/bash
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "First run: creating the Python environment..."
  python3 -m venv .venv
  source .venv/bin/activate
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
else
  source .venv/bin/activate
fi
if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  echo
  echo "config.yaml was created. Open it in TextEdit, fill in the PASTE_ values, then run this again."
  exit 0
fi
python -m frenzo "$@"
