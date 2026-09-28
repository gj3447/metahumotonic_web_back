#!/usr/bin/env bash
# Root-only helper. It passes a strict read-only subset of a root-owned env
# directly to Docker and never echoes or persists its values.
set -Eeuo pipefail

action="${1:-}"; commit="${2:-}"; nonce="${3:-}"; archive="${4:-}"; legacy="${5:-}"; env_file="${6:-}"
owner="com.metahumotonic.ts-shadow"
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
[[ "$action" =~ ^(run|status|cleanup)$ ]] || fail 'usage: run|status|cleanup COMMIT40 NONCE32 [archive legacy env-file]'
[[ "$commit" =~ ^[0-9a-f]{40}$ && "$nonce" =~ ^[0-9a-f]{32}$ ]] || fail 'invalid commit or nonce'
[[ -z "$legacy" || "$legacy" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] || fail 'unsafe legacy container'
[[ -z "$env_file" || "$env_file" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail 'unsafe environment file'
name="mhb-ts-shadow-${commit:0:12}-${nonce:0:12}"
tag="mhb-ts-shadow:${commit:0:12}-${nonce:0:12}"
work="/var/tmp/mhb-ts-shadow-${commit:0:12}-${nonce:0:12}"

owned() {
  [[ -n "$(docker container ls -aq --filter "name=^/${name}$")" ]] || return 0
  [[ "$(docker inspect --format "{{ index .Config.Labels \"${owner}\" }}" "$name")" == "$nonce" ]] || fail 'foreign shadow container name'
}
cleanup() {
  if [[ -n "$(docker container ls -aq --filter "name=^/${name}$")" ]]; then owned; docker rm -f "$name" >/dev/null; fi
  if [[ -n "$(docker image ls -q "$tag")" ]]; then
    [[ "$(docker image inspect --format "{{ index .Config.Labels \"${owner}\" }}" "$tag")" == "$nonce" ]] || fail 'foreign shadow image tag'
    docker image rm "$tag" >/dev/null
  fi
  rm -rf -- "$work"
}
case "$action" in
  cleanup) cleanup; printf '%s\n' '{"status":"CLEANED"}'; exit 0 ;;
  status)
    if [[ -n "$(docker container ls -aq --filter "name=^/${name}$")" ]]; then owned; docker inspect --format '{"resource":"container","running":{{.State.Running}}}' "$name"; else printf '%s\n' '{"resource":"container","state":"absent"}'; fi
    exit 0 ;;
esac

[[ "$archive" == "$work/source.tar" && -f "$archive" && -n "$legacy" && -n "$env_file" ]] || fail 'run requires owned archive, Python container, and env file'
[[ "$(stat -c '%U:%G:%a' "$env_file")" == root:root:600 ]] || fail 'runtime environment file must be root:root:600'
docker inspect "$legacy" >/dev/null 2>&1 || fail 'Python owner container not found'
[[ "$(docker inspect --format '{{.State.Running}}' "$legacy")" == true ]] || fail 'Python owner container not running'
legacy_command="$(docker inspect --format '{{json .Config.Cmd}}' "$legacy")"
[[ "$legacy_command" == *uvicorn* && "$legacy_command" == *app.main:app* ]] || fail 'legacy target is not Python app.main owner'
command -v docker >/dev/null && docker info >/dev/null || fail 'Docker unavailable'

# Only direct read dependencies are allowed into the temporary process.  In
# particular no feedback admin/KG write/platform write/MCP upstream/Wiki DB or
# session secret is passed.  This stream is consumed by Docker; no env copy is
# written to disk and values never appear in receipt or stdout.
selected_env() {
  awk -F= '
    BEGIN { split("MHB_NEO4J_URI MHB_NEO4J_FALLBACK_URIS MHB_NEO4J_USER MHB_NEO4J_PASSWORD MHB_NEO4J_DATABASE MHB_NEO4J_LIVE MHB_MONGO_URI MHB_MONGO_DB MHB_MCP_REGISTRY_COLLECTION MHB_MCP_REGISTRY_CACHE_TTL_SECONDS MHB_REDIS_URL MHB_PLATFORM_DATABASE_URL", keys, " "); for (i in keys) allowed[keys[i]]=1 }
    /^[A-Za-z_][A-Za-z0-9_]*=/ { if (allowed[$1]) print $0 }
  ' "$env_file"
}
platform_pg=0
grep -q '^MHB_PLATFORM_DATABASE_URL=.' "$env_file" && platform_pg=1 || true
cleanup || true
trap 'cleanup || printf "FAIL shadow cleanup requires: %s cleanup %s %s\n" "$0" "$commit" "$nonce" >&2' EXIT
tar -xf "$archive" -C "$work"
[[ -f "$work/Dockerfile" && -f "$work/ts/package-lock.json" ]] || fail 'incomplete exact source archive'
docker build --pull=false --label "${owner}=${nonce}" --label "org.opencontainers.image.revision=${commit}" --tag "$tag" "$work" >/dev/null
read_key="$(openssl rand -hex 32)"
docker create --name "$name" --label "${owner}=${nonce}" --network "container:${legacy}" --memory 512m --memory-swap 512m --cpus 1 --pids-limit 128 --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --env-file <(selected_env) \
  --env MHB_HOST=127.0.0.1 --env MHB_PORT=18081 --env MHB_LOG_JSON=false --env MHB_SHADOW_READ_ONLY=true \
  --env MHB_LEGACY_ORIGIN=http://127.0.0.1:8000 --env MHB_LEGACY_REQUIRED=true --env MHB_WIKI_PUBLIC_WRITES=false \
  --env MHB_PLATFORM_READ_KEY="$read_key" --env MHB_PLATFORM_WRITE_KEY= --env MHB_PLATFORM_DATABASE_REQUIRED="$platform_pg" \
  --env MHB_KG_READ_KEY= --env MHB_KG_WRITE_KEY= --env MHB_FEEDBACK_ADMIN_KEY= "$tag" >/dev/null
owned; docker start "$name" >/dev/null
for _ in $(seq 1 40); do docker exec "$name" node -e "fetch('http://127.0.0.1:18081/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" >/dev/null 2>&1 && break; sleep 1; done

CANARY_READ_KEY="$read_key" CANARY_PLATFORM_PG="$platform_pg" docker exec -i -e CANARY_READ_KEY -e CANARY_PLATFORM_PG "$name" node --input-type=module - <<'NODE'
const base = 'http://127.0.0.1:18081'
const json = async (path, init = {}) => { const r = await fetch(base + path, { ...init, signal: AbortSignal.timeout(4000) }); let b; try { b = await r.json() } catch { b = null }; return { r, b } }
const sameDelegation = async (path) => {
  const direct = await fetch('http://127.0.0.1:8000' + path, { signal: AbortSignal.timeout(4000) })
  const proxied = await fetch(base + path, { signal: AbortSignal.timeout(4000) })
  if (direct.status !== proxied.status || proxied.headers.get('x-mhb-service') !== 'legacy-domain') throw new Error(`delegation mismatch: ${path}`)
}
const ready = await json('/ready')
if (!ready.r.ok || !ready.b?.kg_live || !ready.b?.mongo_required || !ready.b?.mongo_live || !ready.b?.redis_required || !ready.b?.redis_live) throw new Error('required native readiness failed')
const research = await json('/api/research/summary')
if (!research.r.ok || research.b?.source !== 'live') throw new Error('research is not live')
const registry = await json('/api/mcp/servers')
if (!registry.r.ok || registry.b?.source !== 'live' || !Number.isSafeInteger(registry.b?.count)) throw new Error('Mongo MCP registry readback failed')
await sameDelegation('/api/wiki/v1/pages?limit=1')
await sameDelegation('/api/v1/ontology/schema')
if (process.env.CANARY_PLATFORM_PG === '1') {
  const pg = await json('/api/platform/v1/programs', { headers: { Authorization: `Bearer ${process.env.CANARY_READ_KEY}` } })
  if (!pg.r.ok || pg.b?.source !== 'postgres') throw new Error('platform PostgreSQL readback failed')
} else if (ready.b?.platform_postgres_required || ready.b?.platform_postgres_live) throw new Error('unexpected platform PostgreSQL state')
const blocked = await fetch(base + '/api/feedback', { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' })
if (blocked.status !== 405) throw new Error('shadow mutation guard failed')
NODE
# The only receipt is structural: no origin, configuration, body, or secret.
printf '{"schema":"metahumotonic/ts-shadow-canary@1","status":"PASS","commit":"%s","checks":["kg-research-live","mongo-registry-read","redis-readiness","wiki-delegation","ontology-delegation","shadow-mutation-block"],"platformPostgres":"%s","publicIngressChanged":false,"databaseWrites":false}\n' "$commit" "$([[ "$platform_pg" == 1 ]] && printf readback || printf not-configured)"
