#!/bin/bash
if [ -f icon-source.png ]; then
  PY=$(command -v python3 || command -v python)
  $PY -m pip install pillow || $PY -m pip install --break-system-packages pillow
  $PY ci/mkicons.py
else
  echo "no icon-source.png"
fi
