#!/usr/bin/env bash
# Pin this project's tools without replacing any system-wide runtime.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
version="$(cat "$root/.node-version")"
case "$(uname -m)" in x86_64) arch=x64;; aarch64|arm64) arch=arm64;; *) echo 'Unsupported architecture' >&2; exit 78;; esac
case "$(uname -s)" in Linux) os=linux;; Darwin) os=darwin;; *) echo 'Unsupported platform' >&2; exit 78;; esac
selected=''
for candidate in "${MHB_NODE_HOME:-/nonexistent}/bin/node" "/opt/metahumotonic/toolchains/node-v${version}-${os}-${arch}/bin/node" "$HOME/.nvm/versions/node/v${version}/bin/node" "$(command -v node || true)"; do
  if [[ -x "$candidate" ]] && [[ "$("$candidate" --version)" == "v$version" ]]; then selected="$candidate"; break; fi
done
if [[ -z "$selected" ]]; then echo "Node $version required. Use nvm install or set MHB_NODE_HOME to that installation." >&2; exit 78; fi
export PATH="$(dirname "$selected"):$PATH"
command="${1:-node}"
if (($#)); then shift; fi
case "$command" in
  node) exec "$selected" "$@";;
  npm|npx) exec "$selected" "$(dirname "$selected")/../lib/node_modules/npm/bin/${command}-cli.js" "$@";;
  *) echo 'Usage: scripts/with-node.sh node|npm|npx [arguments]' >&2; exit 64;;
esac
