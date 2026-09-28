#!/usr/bin/env bash
# Local build and readback only. Never publishes, changes ingress, or writes KG.
set -euo pipefail
if (( $# < 1 || $# > 2 )); then
  echo 'Usage: bash ops/verify-company-hub.sh /path/to/metahumotonic-web [new-receipt.json]' >&2
  exit 64
fi
backend_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
frontend_root="$(cd "$1" && pwd)"
[[ -f "$frontend_root/src/data/learning-hub.json" ]] || { echo 'Missing frontend public snapshot' >&2; exit 1; }
receipt_args=()
if (( $# == 2 )); then
  receipt_path="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$2")"
  [[ ! -e "$receipt_path" ]] || { echo 'Refusing to overwrite a verification receipt' >&2; exit 1; }
  receipt_args=(--receipt "$receipt_path")
fi
bash "$backend_root/ts/scripts/with-node.sh" npm --prefix "$backend_root/ts" run build
bash "$backend_root/ts/scripts/with-node.sh" node "$backend_root/ts/scripts/export-learning-hub.mjs" "$frontend_root/src/data/learning-hub.json" --check
(
  cd "$frontend_root"
  bash scripts/with-node.sh npm run build
  python3 scripts/verify/learning_publication.py "${receipt_args[@]}"
)
