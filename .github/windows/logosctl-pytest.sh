#!/usr/bin/env bash
# The logosctl client's integration suite against the staged Windows
# logosctl.exe, over every transport. Run by windows.yml from the stage root.
# The unit suite stays on Linux and macOS: its fakes are POSIX shell scripts.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python -m pip install --quiet --disable-pip-version-check pytest

export LOGOSCTL_BIN="$STAGE_ABS/ctl/bin/logosctl.exe"
export LOGOSCTL_TEST_MODULES_DIR="$STAGE_ABS/test-modules/modules"
export PYTHONPATH="$repo/src"
cd "$repo"
for transport in local tcp tcp_ssl; do
  echo "--- logosctl integration over $transport"
  python -m pytest -q -p no:cacheprovider tests/logosctl/integration --transport="$transport"
done
