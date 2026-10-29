#!/bin/bash
cd "$(dirname "$0")" || exit 1
clear
if [ ! -f .venv/bin/activate ]; then
  echo "Setup hasn't been run yet."
  echo 'Close this and double-click "1 - SETUP" first.'
  echo
  read -n 1 -s -r -p "Press any key to close..."; exit 1
fi
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install --quiet -r requirements.txt 2>/dev/null

echo "==============================================="
echo "  edgar_analyst"
echo "==============================================="
echo
echo "Type the tickers you want, separated by spaces."
echo "  e.g.   ULTA              (one company)"
echo "         VST CEG NRG TLN   (a peer group + comps sheet)"
echo
read -r -p "Tickers: " TICKERS
if [ -z "$TICKERS" ]; then
  echo "Nothing entered."; read -n 1 -s -r -p "Press any key to close..."; exit 1
fi
echo
echo "Comparing 10-K filings too? That's the best part but adds ~15s per"
echo "company the first time (it downloads two annual reports each)."
read -r -p "Include 10-K comparison? [Y/n]: " DIFFS
EXTRA=""
case "$DIFFS" in [Nn]*) EXTRA="--no-filings";; esac
echo
python -m edgar_analyst $TICKERS --comps --csv $EXTRA
echo
echo "Opening the output folder..."
open output 2>/dev/null
echo
read -n 1 -s -r -p "Press any key to close..."
