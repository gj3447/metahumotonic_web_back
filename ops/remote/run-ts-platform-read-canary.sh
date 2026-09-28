#!/usr/bin/env bash
# Root-only data-01 helper. A private container shares PostgreSQL's network namespace and publishes no port.
set -Eeuo pipefail
mode="${1:-status}"; image="${2:-}"; commit="${3:-}"; nonce="${4:-}"; source_digest="${5:-}"; root="${6:-/var/lib/metahumotonic-platform/read-canary}"
container="postgresql"; reader_receipt="/var/lib/metahumotonic-platform/read-canary-reader/receipt.json"; password_file="/etc/metahumotonic/platform-read-canary/reader-password"
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
[[ "$mode" =~ ^(status|run|cleanup)$ ]] || fail 'invalid mode'
[[ "$image" =~ ^sha256:[0-9a-f]{64}$ && "$commit" =~ ^[0-9a-f]{40}$ && "$nonce" =~ ^[0-9a-f]{24}$ && "$source_digest" =~ ^[0-9a-f]{64}$ && "$root" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail 'invalid exact image, commit, nonce or root'
name="mhb-platform-read-$nonce"; work="$root/$nonce"; receipt="$work/receipt.json"
command -v docker >/dev/null || fail 'docker is required'; docker inspect "$container" >/dev/null 2>&1 || fail 'PostgreSQL container not found'
if [[ "$mode" != cleanup ]]; then
  command -v openssl >/dev/null || fail 'openssl is required'
  image_id="$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null || true)"; [[ "$image_id" == "$image" ]] || fail 'exact image ID is not locally present'
  revision="$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image")"; [[ "$revision" == "$commit" ]] || fail 'image revision label does not match requested commit'
  source_label="$(docker image inspect --format '{{ index .Config.Labels "com.metahumotonic.source-archive-sha256" }}' "$image")"; [[ "$source_label" == "$source_digest" ]] || fail 'image source archive digest does not match requested commit'
fi
[[ "$mode" == cleanup ]] || { [[ -f "$reader_receipt" && -f "$password_file" ]] || fail 'reader role receipt or root-only secret missing'; [[ "$(stat -c '%U:%G:%a' "$reader_receipt")" == root:root:600 && "$(stat -c '%U:%G:%a' "$password_file")" == root:root:600 ]] || fail 'reader artifacts must be root:root 0600'; }
if [[ "$mode" == run ]]; then
  python3 - "$reader_receipt" <<'PY' || fail 'reader receipt is not verified'
import json, pathlib, sys
receipt = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
assert receipt.get('schema') == 'metahumotonic/platform-read-canary-reader@1'
assert receipt.get('status') == 'PROVISIONED' and receipt.get('privilegeDenialVerified') is True
assert receipt.get('database') == 'metahumotonic_platform' and receipt.get('readerRole') == 'mhb_platform_shadow_reader'
PY
  role_flags="$(docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -Atqc "SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolinherit FROM pg_roles WHERE rolname='mhb_platform_shadow_reader'")"
  [[ "$role_flags" == 'f|f|f|f|f|f' ]] || fail 'reader role flags changed'
  reader_grants="$(docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d metahumotonic_platform -Atqc "SELECT has_database_privilege('mhb_platform_shadow_reader','metahumotonic_platform','CONNECT'),has_database_privilege('mhb_platform_shadow_reader','metahumotonic_platform','TEMPORARY'),has_database_privilege('mhb_platform_shadow_reader','metahumotonic_platform','CREATE'),has_schema_privilege('mhb_platform_shadow_reader','mhb_platform','USAGE'),has_schema_privilege('mhb_platform_shadow_reader','mhb_platform','CREATE'),bool_and(has_table_privilege('mhb_platform_shadow_reader',format('%I.%I',schemaname,tablename),'SELECT')),bool_or(has_table_privilege('mhb_platform_shadow_reader',format('%I.%I',schemaname,tablename),'INSERT') OR has_table_privilege('mhb_platform_shadow_reader',format('%I.%I',schemaname,tablename),'UPDATE') OR has_table_privilege('mhb_platform_shadow_reader',format('%I.%I',schemaname,tablename),'DELETE') OR has_table_privilege('mhb_platform_shadow_reader',format('%I.%I',schemaname,tablename),'TRUNCATE')) FROM pg_tables WHERE schemaname='mhb_platform'")"
  [[ "$reader_grants" == 't|f|f|t|f|t|f' ]] || fail 'reader privileges changed'
fi
container_is_owned() {
  [[ "$(docker inspect --format '{{ index .Config.Labels "metahumotonic.owner" }}' "$name")" == platform-read-canary ]] || return 1
  [[ "$(docker inspect --format '{{ index .Config.Labels "metahumotonic.nonce" }}' "$name")" == "$nonce" ]]
}
remove_owned_container() {
  if docker inspect "$name" >/dev/null 2>&1; then container_is_owned || fail 'refusing to remove non-canary or nonce-mismatched container'; docker rm -f "$name" >/dev/null; fi
}
cleanup_owned() {
  remove_owned_container
  if [[ -e "$work" ]]; then
    [[ -d "$work" && ! -L "$work" && "$(stat -c '%U:%G:%a' "$work")" == root:root:700 ]] || fail 'refusing to remove foreign canary work path'
    rm -rf -- "$work"
  fi
}
if [[ "$mode" == status ]]; then printf '{"schema":"metahumotonic/platform-read-canary@1","mode":"status","image":"%s","commit":"%s","publishedPorts":false,"databaseWrites":false,"publicIngressChanged":false}\n' "$image" "$commit"; exit 0; fi
if [[ "$mode" == cleanup ]]; then cleanup_owned; printf '{"schema":"metahumotonic/platform-read-canary@1","mode":"cleanup","containerRemoved":true,"imageRemoved":false}\n'; exit 0; fi
[[ ! -e "$work" ]] || fail 'nonce work path exists'; install -d -m 700 -o root -g root "$work"
trap 'trap - EXIT; cleanup_owned || { printf \"FAIL canary cleanup failed\\n\" >&2; exit 1; }' EXIT
password="$(cat "$password_file")"; [[ "$password" =~ ^[0-9a-f]{64}$ ]] || fail 'invalid reader secret'
read_key="$(openssl rand -hex 32)"; env_file="$work/env"; umask 077
cat >"$env_file" <<ENV
MHB_PLATFORM_DATABASE_URL=postgresql://mhb_platform_shadow_reader:$password@127.0.0.1:5432/metahumotonic_platform
MHB_PLATFORM_DATABASE_REQUIRED=true
MHB_PLATFORM_READ_KEY=$read_key
MHB_PLATFORM_WRITE_KEY=
MHB_SHADOW_READ_ONLY=true
MHB_MONGO_URI=
MHB_REDIS_URL=
MHB_NEO4J_LIVE=false
MHB_NEO4J_URI=
MHB_NEO4J_USER=
MHB_NEO4J_PASSWORD=
MHB_LEGACY_REQUIRED=false
MHB_LEGACY_ORIGIN=
MHB_ONTOLOGY_ENABLED=false
MHB_WIKI_PUBLIC_WRITES=false
MHB_WIKI_REQUIRE_REDIS=false
MHB_HOST=127.0.0.1
MHB_PORT=18082
ENV
chmod 600 "$env_file"
docker create --name "$name" --network "container:$container" --no-healthcheck --read-only --tmpfs /tmp:rw,noexec,nosuid,size=32m --cap-drop ALL --security-opt no-new-privileges --pids-limit 128 --memory 512m --cpus 1 --env-file "$env_file" --label metahumotonic.owner=platform-read-canary --label "metahumotonic.nonce=$nonce" "$image" >/dev/null
docker start "$name" >/dev/null
ready=false
for _ in $(seq 1 20); do
  if timeout 6 docker exec "$name" node -e 'fetch("http://127.0.0.1:18082/ready",{signal:AbortSignal.timeout(4000)}).then(async r=>{const b=await r.json();if(r.ok&&b.platform_postgres_live===true&&b.platform_postgres_required===true)process.exit(0);process.exit(1)}).catch(()=>process.exit(1))' >/dev/null 2>&1; then ready=true; break; fi
  sleep 1
done
[[ "$ready" == true ]] || fail 'ready did not pass'
timeout 8 docker exec "$name" node -e 'fetch("http://127.0.0.1:18082/api/platform/v1/programs",{signal:AbortSignal.timeout(4000),headers:{"x-api-key":process.env.MHB_PLATFORM_READ_KEY}}).then(async r=>{const b=await r.json();process.exit(r.ok&&b.source==="postgres"?0:1)}).catch(()=>process.exit(1))'
timeout 8 docker exec "$name" node -e 'fetch("http://127.0.0.1:18082/api/platform/v1/observations",{method:"POST",signal:AbortSignal.timeout(4000),headers:{"x-api-key":process.env.MHB_PLATFORM_READ_KEY,"content-type":"application/json"},body:"{}"}).then(r=>process.exit(r.status===405?0:1)).catch(()=>process.exit(1))'
remove_owned_container
rm -f "$env_file"
printf '{"schema":"metahumotonic/platform-read-canary@1","status":"PASS","commit":"%s","image":"%s","ready":true,"postgresRead":true,"mutationBlocked":true,"publishedPorts":false,"databaseWrites":false,"publicIngressChanged":false,"secretMaterialPrinted":false}\n' "$commit" "$image" >"$receipt"; chmod 600 "$receipt"; trap - EXIT
printf '%s\n' "$(cat "$receipt")"
