#!/usr/bin/env bash
# The gate: unit tests, shell lint, and a scan for private data in tracked files.
# The pre-push hook runs it. Nothing runs on GitHub.
#
#   ./check.sh          run everything
#   ./check.sh usage    print key=value usage counters
set -uo pipefail
cd "$(dirname "$0")" || exit 1

if [ "${1:-}" = "usage" ]; then
  log="$HOME/.local/share/filmare/renders.log"
  if [ -f "$log" ]; then
    echo "renders=$(wc -l < "$log" | tr -d ' ')"
  else
    echo "renders=0"
  fi
  exit 0
fi

failed=0

echo "== unit tests"
python3 -m unittest discover -s tests -t . 2>&1 | tail -4
[ "${PIPESTATUS[0]}" -eq 0 ] || failed=1

echo "== shellcheck"
if command -v shellcheck >/dev/null; then
  # The leak gate file is Teyla's and is not a shell script.
  git ls-files -co --exclude-standard '*.sh' '.githooks/*' bin/filmare | grep -v '\.teyla-leak-gate$' | xargs shellcheck || failed=1
else
  echo "SKIPPED: shellcheck is not installed"
fi

echo "== private data in tracked files"
# This repo is public. Fixtures use invented data, and real names live in the user's config.
if git ls-files -co --exclude-standard | grep -v '^check.sh$' \
    | xargs grep -nIE "$(id -un)|-----BEGIN [A-Z ]*PRIVATE KEY" 2>/dev/null; then
  echo "FAILED: the lines above must not be published"
  failed=1
else
  echo "clean"
fi

if [ "$failed" -ne 0 ]; then
  echo "check.sh: FAILED"
  exit 1
fi
echo "check.sh: passed"
