#!/usr/bin/env bash
# Runs the repository checks, from the repository root: the lint and test jobs of
# .github/workflows/ci.yml (the add-on manifest lint and the image build only run in CI).
# Every check runs even if an earlier one failed; the exit status is non-zero if any failed.
cd "$(dirname "$0")/.." || exit 2

status=0
check() {
    printf '\n==> %s\n' "$*"
    "$@" || { echo "FAILED: $*"; status=1; }
}

check ruff check --no-cache .
# SC2155 (export VAR="$(...)") is the standard idiom in Home Assistant add-ons.
check shellcheck -e SC2155 hypon-local-addon/run.sh dev/check.sh
check yamllint .
check python -W error::ResourceWarning -m coverage run -m unittest discover -s tests
check coverage report

echo
if [ "${status}" -eq 0 ]; then echo "All checks passed."; else echo "Some checks FAILED."; fi
exit "${status}"
