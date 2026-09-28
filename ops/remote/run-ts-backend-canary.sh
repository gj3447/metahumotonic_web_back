#!/usr/bin/env bash
# Root-only helper for an isolated TS gateway canary on VM100.
#
# It never changes ingress, existing containers, databases, Mongo or Docker
# networks. The new container shares a *read-only client* network namespace
# with one existing Python replica solely to verify the fixed legacy proxy.
set -Eeuo pipefail

action="${1:-}"
commit="${2:-}"
nonce="${3:-}"
archive="${4:-}"
legacy="${5:-}"
owner="com.metahumotonic.ts-canary"

fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
[[ "$action" =~ ^(run|status|cleanup)$ ]] || fail 'usage: run|status|cleanup COMMIT40 NONCE32 [archive legacy-container]'
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || fail 'invalid exact commit'
[[ "$nonce" =~ ^[0-9a-f]{32}$ ]] || fail 'invalid nonce'
[[ -z "$legacy" || "$legacy" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] || fail 'unsafe legacy container name'

name="mhb-ts-canary-${commit:0:12}-${nonce:0:12}"
tag="mhb-ts-canary:${commit:0:12}-${nonce:0:12}"
work="/var/tmp/mhb-ts-canary-${commit:0:12}-${nonce:0:12}"

owned_container() {
  [[ -n "$(docker container ls -aq --filter "name=^/${name}$")" ]] || return 0
  [[ "$(docker container inspect --format "{{ index .Config.Labels \"${owner}\" }}" "$name")" == "$nonce" ]] \
    || fail 'refusing a foreign container with the canary name'
}
owned_image() {
  [[ -n "$(docker image ls -q "$tag")" ]] || return 0
  [[ "$(docker image inspect --format "{{ index .Config.Labels \"${owner}\" }}" "$tag")" == "$nonce" ]] \
    || fail 'refusing a foreign image with the canary tag'
}
cleanup_resources() {
  local failed=false
  if [[ -n "$(docker container ls -aq --filter "name=^/${name}$")" ]]; then
    owned_container
    docker rm -f "$name" >/dev/null || failed=true
  fi
  if [[ -n "$(docker image ls -q "$tag")" ]]; then
    owned_image
    docker image rm "$tag" >/dev/null || failed=true
  fi
  [[ "$failed" == false ]]
}
cleanup() {
  cleanup_resources || return 1
  rm -rf -- "$work"
}
status() {
  if [[ -n "$(docker container ls -aq --filter "name=^/${name}$")" ]]; then
    owned_container
    docker inspect --format '{"resource":"container","state":"{{.State.Status}}","running":{{.State.Running}}}' "$name"
  else
    printf '%s\n' '{"resource":"container","state":"absent"}'
  fi
  if [[ -n "$(docker image ls -q "$tag")" ]]; then
    owned_image
    docker image inspect --format '{"resource":"image","id":"{{.Id}}"}' "$tag"
  else
    printf '%s\n' '{"resource":"image","state":"absent"}'
  fi
}

case "$action" in
  cleanup) cleanup; printf '%s\n' '{"status":"CLEANED"}'; exit 0 ;;
  status) status; exit 0 ;;
esac

[[ -n "$archive" && -n "$legacy" ]] || fail 'run requires source archive and legacy container'
[[ "$archive" == "$work/source.tar" && -f "$archive" ]] || fail 'unexpected source archive path'
[[ -d "$work" ]] || fail 'missing owned canary workspace'
command -v docker >/dev/null || fail 'docker is required'
docker info >/dev/null || fail 'docker daemon unavailable'

# The existing container is never modified. Check that the explicit target is
# one of the expected Python domain owners before sharing its namespace.
docker inspect "$legacy" >/dev/null 2>&1 || fail 'legacy container not found'
[[ "$(docker inspect --format '{{.State.Running}}' "$legacy")" == true ]] || fail 'legacy container is not running'
legacy_command="$(docker inspect --format '{{json .Config.Cmd}}' "$legacy")"
[[ "$legacy_command" == *uvicorn* && "$legacy_command" == *app.main:app* ]] || fail 'legacy target is not the Python domain container'

cleanup_resources || fail 'could not clean a prior owned canary'
trap 'cleanup || printf "FAIL canary cleanup requires: %s cleanup %s %s\n" "$0" "$commit" "$nonce" >&2' EXIT

tar -xf "$archive" -C "$work"
[[ -f "$work/Dockerfile" && -f "$work/ts/package-lock.json" ]] || fail 'incomplete exact source archive'
docker build --pull=false --label "${owner}=${nonce}" --label "org.opencontainers.image.revision=${commit}" \
  --tag "$tag" "$work" >/dev/null 2>&1 || fail 'exact canary image build failed'
owned_image

# No env-file is loaded. This prevents the canary from receiving production
# data-store, KG, upstream MCP or credential configuration. It has no published
# port and cannot write through the platform API because this run uses only the
# generated read key.
read_key="$(openssl rand -hex 32)"
docker create --name "$name" --label "${owner}=${nonce}" --network "container:${legacy}" \
  --memory 512m --memory-swap 512m --cpus 1 --pids-limit 128 --read-only --cap-drop ALL \
  --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --env MHB_HOST=127.0.0.1 --env MHB_PORT=18080 --env MHB_LOG_JSON=false \
  --env MHB_LEGACY_ORIGIN=http://127.0.0.1:8000 --env MHB_LEGACY_REQUIRED=true \
  --env MHB_NEO4J_LIVE=false --env MHB_MONGO_URI= --env MHB_REDIS_URL= \
  --env MHB_PLATFORM_DATABASE_URL= --env MHB_PLATFORM_DATABASE_REQUIRED=false \
  --env "MHB_PLATFORM_READ_KEY=${read_key}" --env MHB_PLATFORM_WRITE_KEY= \
  "$tag" >/dev/null
owned_container
docker start "$name" >/dev/null

for _ in $(seq 1 40); do
  if docker exec "$name" node -e "fetch('http://127.0.0.1:18080/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" >/dev/null 2>&1; then break; fi
  sleep 1
done

# The probe executes inside the un-published canary namespace. It makes only
# GET/JSON-RPC initialize calls; no feedback, observation, registry or MCP
# upstream mutation is invoked. The receipt deliberately contains no headers,
# credentials, connection strings, response bodies or container environment.
CANARY_READ_KEY="$read_key" docker exec -e CANARY_READ_KEY "$name" node --input-type=module - <<'NODE'
const base = 'http://127.0.0.1:18080'
const { createHash } = await import('node:crypto')
const get = async (path, headers = {}) => {
  const response = await fetch(path.startsWith('http://') || path.startsWith('https://') ? path : base + path, { headers, signal: AbortSignal.timeout(3000) })
  if (!response.ok) throw new Error(`${path} returned ${response.status}`)
  return response
}
await get('/health')
await get('/ready')
const legacyOrigin = 'http://127.0.0.1:8000'
const parityHeaders = ['content-type', 'cache-control', 'etag', 'location', 'www-authenticate']
const digest = async (response) => {
  const hash = createHash('sha256')
  const reader = response.body?.getReader()
  if (!reader) return hash.digest('hex')
  let size = 0
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    size += value.byteLength
    if (size > 1_048_576) {
      await reader.cancel()
      throw new Error('delegated response exceeds parity bound')
    }
    hash.update(value)
  }
  return hash.digest('hex')
}
const delegated = async (origin, path, method) => fetch(origin + path, {
  method, redirect: 'manual', signal: AbortSignal.timeout(3000)
})
const readOnlyParity = async (path, method) => {
  const [direct, proxied] = await Promise.all([delegated(legacyOrigin, path, method), delegated(base, path, method)])
  if (direct.status !== proxied.status) throw new Error(`delegation status mismatch for ${method} ${path}`)
  if (proxied.headers.get('x-mhb-service') !== 'legacy-domain') throw new Error(`delegation marker missing for ${method} ${path}`)
  for (const header of parityHeaders) if (direct.headers.get(header) !== proxied.headers.get(header)) {
    throw new Error(`delegation header mismatch for ${method} ${path}`)
  }
  if (method === 'GET' && await digest(direct) !== await digest(proxied)) throw new Error(`delegation body mismatch for ${path}`)
}
// Fixed public reads only: no session, CSRF, idempotency, moderation or data mutation.
for (const path of ['/api/wiki/v1', '/api/wiki/v1/pages?limit=1', '/api/wiki/v1/pages/mhb-read-parity-missing', '/api/v1/ontology/schema']) {
  await readOnlyParity(path, 'GET')
  await readOnlyParity(path, 'HEAD')
}
await get('/api/public/v1/hub')
const key = process.env.CANARY_READ_KEY
const anonymous = await fetch(base + '/api/platform/v1/programs')
if (anonymous.status !== 401) throw new Error(`platform anonymous status ${anonymous.status}`)
await get('/api/platform/v1/programs', { Authorization: `Bearer ${key}` })
const mcp = await fetch(base + '/mcp', { method: 'POST', headers: { Authorization: `Bearer ${key}`, 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'vm100-ts-canary', version: '1' } } }), signal: AbortSignal.timeout(3000) })
if (!mcp.ok || (await mcp.json()).result?.serverInfo?.name !== 'metahumotonic-platform') throw new Error('MCP read initialization failed')
NODE

image_id="$(docker image inspect --format '{{.Id}}' "$tag")"
printf '{"schema":"metahumotonic/ts-canary@1","status":"PASS","commit":"%s","image":"%s","legacyContainer":"%s","checks":["ready","wiki-ontology-read-parity","public-hub","platform-auth","mcp-initialize"],"publishedPorts":false,"databaseWrites":false,"publicIngressChanged":false}\n' \
  "$commit" "$image_id" "$legacy"
