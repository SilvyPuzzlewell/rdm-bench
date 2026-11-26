#!/usr/bin/env bash
# Compare baseline and patched on the database prepare.sh generated; the summary lands in results/.
# Arguments go to compare (see compare --help), e.g.
#   ./bench.sh --patched-src ../invenio-records-permissions --runs 4 --requests search=200 read=300
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
PY=${PY:-$(cd "$DIR/../inveniordm-reference-repo" && pwd)/.venv/bin/python}

"$PY" "$DIR/rdm_permissions_bench.py" compare "$@"
