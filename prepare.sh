#!/usr/bin/env bash
# Install the reference repo's venv, start the services, then wipe the instance and generate the benchmark database.
# Arguments go to setup, e.g. ./prepare.sh --records 1000 --communities 20 (see setup --help).
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIR/../inveniordm-reference-repo" && pwd)
PY=${PY:-$REPO/.venv/bin/python}

# uWSGI only serves a deployed site; the benchmark runs the app in process.
uv sync --project "$REPO" --extra tests \
  --no-install-package uwsgi --no-install-package uwsgitop --no-install-package uwsgi-tools
"$PY" "$DIR/rdm_permissions_bench.py" services-up
"$PY" "$DIR/rdm_permissions_bench.py" setup "$@"
