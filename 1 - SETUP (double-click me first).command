#!/bin/bash
# One-time setup. Double-click this, then use "2 - RUN".
cd "$(dirname "$0")" || exit 1
clear
echo "==============================================="
echo "  edgar_analyst - one-time setup"
echo "==============================================="
echo

PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
if [ -z "$PYV" ]; then
  echo "Python 3 isn't installed yet."
  echo "macOS may have just opened a window offering to install"
  echo "developer tools -- click Install, wait for it to finish,"
  echo "then double-click this file again."
  echo
  echo "If no window appeared, download Python from python.org"
  echo "and run this again."
  echo
  read -n 1 -s -r -p "Press any key to close..."; exit 1
fi

MAJOR=${PYV%%.*}; MINOR=${PYV##*.}
if [ "$MAJOR" -lt 3 ] || { [ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 10 ]; }; then
  echo "Found Python $PYV, but this needs 3.10 or newer."
  echo "Install a current version from python.org, then run this again."
  echo
  read -n 1 -s -r -p "Press any key to close..."; exit 1
fi
echo "Python $PYV -- good."
echo

if [ ! -s sec_user_agent.txt ] && [ -z "$SEC_USER_AGENT" ]; then
  echo "The SEC requires every program that downloads its data to"
  echo "identify itself with a real name and email."
  echo
  read -r -p "Your name and email (e.g. Jane Doe jane@example.com): " UA
  if [ -z "$UA" ]; then
    echo "Nothing entered. Run this again when you're ready."
    read -n 1 -s -r -p "Press any key to close..."; exit 1
  fi
  printf '# Your name and email for SEC requests. Git-ignored -- never uploaded.\n%s\n' "$UA" > sec_user_agent.txt
  echo "Saved to sec_user_agent.txt."
  echo
fi

echo "Creating the virtual environment (a private box for this"
echo "project's libraries so nothing else on your Mac is touched)..."
rm -rf .venv
python3 -m venv .venv || { echo; echo "Could not create it."; read -n 1 -s -r -p "Press any key to close..."; exit 1; }
# shellcheck disable=SC1091
. .venv/bin/activate

echo "Installing libraries (takes a minute the first time)..."
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt || { echo; echo "Install failed -- check your internet connection."; read -n 1 -s -r -p "Press any key to close..."; exit 1; }
echo "Done."
echo

echo "==============================================="
echo "  Verifying against real SEC filings"
echo "==============================================="
echo
python selftest.py
echo
echo "If that says everything passed, you're ready."
echo 'Use "2 - RUN" from now on. You never need this file again.'
echo
read -n 1 -s -r -p "Press any key to close..."
