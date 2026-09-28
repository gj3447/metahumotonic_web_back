#!/usr/bin/env bash
# Disposable, commit-bound HTTP/CLI/MCP release canary. Never uses production DB/Redis.
set -Eeuo pipefail

image="${1:-}"
commit="${2:-}"
env_file="${3:-/etc/metahumotonic/web-back.env}"
database="${4:-}"
receipt="${5:-}"
rollout_nonce="${6:-}"
redis_image="${7:-}"
runtime_helper="${8:-}"
gateway_image="${9:-}"
[[ "$image" =~ ^metahumotonic-web-back:[A-Za-z0-9._-]+-x86$ ]]
[[ "$commit" =~ ^[0-9a-f]{40}$ ]]
[[ "$rollout_nonce" =~ ^[0-9a-f]{32}$ ]]
[[ "$database" == "metahumotonic_wiki_canary_${commit:0:12}_${rollout_nonce:0:12}" ]]
[[ "$env_file" =~ ^/[A-Za-z0-9._/-]+$ ]]
[[ "$receipt" =~ ^/[A-Za-z0-9._/-]+$ ]]
[[ "$redis_image" =~ ^redis:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$ ]]
[[ "$runtime_helper" =~ ^/[A-Za-z0-9._/-]+$ ]]
[[ "$gateway_image" =~ ^metahumotonic-web-back-ts:[A-Za-z0-9._-]+-x86$ ]]
test "$(stat -c '%U:%G:%a' "$env_file")" = root:root:600

image_id="$(docker image inspect "$image" --format '{{.Id}}')"
migrations_sha="$(docker image inspect "$image" --format '{{index .Config.Labels "com.metahumotonic.wiki-migrations-sha256"}}')"
gateway_image_id="$(docker image inspect "$gateway_image" --format '{{.Id}}')"
[[ "$migrations_sha" =~ ^[0-9a-f]{64}$ ]]
[[ "$(docker image inspect "$gateway_image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" == "$commit" ]]
if test -f "$receipt"; then
  python3 - "$receipt" "$commit" "$image_id" "$migrations_sha" "$gateway_image_id" <<'PY'
import json, pathlib, sys
body=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert body.get("status") == "PASS"
assert body.get("cleanup") == "PASS"
assert body.get("database_cleanup") == "DATA_HELPER_REQUIRED"
assert body.get("commit") == sys.argv[2]
assert body.get("image_id") == sys.argv[3]
assert body.get("migrations_sha256") == sys.argv[4]
assert body.get("gateway_image_id") == sys.argv[5]
assert {"ts_delegated_browser_session_csrf","ts_delegated_agent_idempotency_cas_direct_readback"}.issubset(body.get("tests", []))
PY
  printf 'PASS existing commit/image-bound synthetic canary receipt %s\n' "$receipt"
  exit 0
fi

short="${commit:0:12}-${rollout_nonce:0:12}"
network="mhb-wiki-canary-$short"
redis_name="mhb-wiki-canary-redis-$short"
app_name="mhb-wiki-canary-app-$short"
gateway_name="mhb-wiki-canary-gateway-$short"
work_dir="$(dirname "$receipt")/.canary-$short"
canary_env="$work_dir/canary.env"
gateway_env="$work_dir/gateway.env"
created_network=false
created_redis=false
created_app=false
cleanup_ok=true
cleanup_gateway() {
  if docker container inspect "$gateway_name" >/dev/null 2>&1; then
    test "$(docker inspect "$gateway_name" --format '{{index .Config.Labels "com.metahumotonic.wiki-canary.commit"}}')" = "$commit"
    test "$(docker inspect "$gateway_name" --format '{{index .Config.Labels "com.metahumotonic.wiki-canary.nonce"}}')" = "$rollout_nonce"
    docker rm -f "$gateway_name" >/dev/null
  fi
  if docker image inspect "$gateway_image" >/dev/null 2>&1; then
    test "$(docker image inspect "$gateway_image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$commit"
    docker image rm "$gateway_image" >/dev/null
  fi
}

cleanup() {
  trap - EXIT ERR
  set +e
  cleanup_gateway || cleanup_ok=false
  bash "$runtime_helper" cleanup "$commit" "$rollout_nonce" || cleanup_ok=false
  [[ "$cleanup_ok" == true ]]
}
trap 'status=$?; cleanup || status=1; exit "$status"' EXIT

bash "$runtime_helper" reserve "$commit" "$rollout_nonce" >/dev/null

for resource in "$app_name" "$redis_name" "$gateway_name"; do
  ! docker container inspect "$resource" >/dev/null 2>&1
done
! docker network inspect "$network" >/dev/null 2>&1
test "$(stat -c '%U:%G:%a' "$work_dir")" = root:root:700
test "$(stat -c '%U:%G:%a' "$(dirname "$receipt")")" = root:root:700

python3 - "$env_file" "$canary_env" "$database" <<'PY'
import os, pathlib, re, sys
source=pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
target=pathlib.Path(sys.argv[2]); database=sys.argv[3]
out=[]
for line in source:
    if line.startswith("MHB_WIKI_DATABASE_URL="):
        line=re.sub(r"/metahumotonic_wiki(?=\?|$)", f"/{database}", line)
    elif line.startswith("MHB_REDIS_URL="):
        line="MHB_REDIS_URL=redis://wiki-canary-redis:6379/0"
    elif line.startswith("MHB_MONGO_URI="):
        line="MHB_MONGO_URI="
    elif line.startswith("MHB_NEO4J_LIVE="):
        line="MHB_NEO4J_LIVE=false"
    elif line.startswith("MHB_NEO4J_URI=") or line.startswith("MHB_NEO4J_PASSWORD=") or line.startswith("MHB_PLATFORM_DATABASE_URL="):
        line=line.split("=",1)[0]+"="
    elif line.startswith("MHB_WIKI_SESSION_COOKIE_SECURE="):
        line="MHB_WIKI_SESSION_COOKIE_SECURE=false"
    out.append(line)
target.write_text("\n".join(out)+"\n", encoding="utf-8")
os.chmod(target, 0o600)
PY
test "$(stat -c '%U:%G:%a' "$canary_env")" = root:root:600
grep -q "^MHB_WIKI_DATABASE_URL=.*@192.168.0.25:5432/${database}?sslmode=require$" "$canary_env"
grep -q '^MHB_REDIS_URL=redis://wiki-canary-redis:6379/0$' "$canary_env"
grep -q '^MHB_MONGO_URI=$' "$canary_env"
grep -q '^MHB_NEO4J_LIVE=false$' "$canary_env"
cat >"$gateway_env" <<ENV
MHB_HOST=0.0.0.0
MHB_PORT=8000
MHB_LOG_JSON=false
MHB_LEGACY_ORIGIN=http://${app_name}:8000
MHB_LEGACY_REQUIRED=true
MHB_WIKI_PUBLIC_WRITES=false
MHB_WIKI_REQUIRE_REDIS=false
MHB_MONGO_URI=
MHB_REDIS_URL=
MHB_NEO4J_LIVE=false
MHB_NEO4J_URI=
MHB_NEO4J_PASSWORD=
MHB_PLATFORM_DATABASE_URL=
MHB_PLATFORM_DATABASE_REQUIRED=false
MHB_PLATFORM_READ_KEY=
MHB_PLATFORM_WRITE_KEY=
ENV
chmod 600 "$gateway_env"
test "$(stat -c '%U:%G:%a' "$gateway_env")" = root:root:600

docker image inspect "$redis_image" >/dev/null 2>&1 || docker pull "$redis_image" >/dev/null
redis_image_id="$(docker image inspect "$redis_image" --format '{{.Id}}')"
redis_digest="${redis_image##*@}"
docker image inspect "$redis_image" --format '{{json .RepoDigests}}' | grep -q "$redis_digest"
docker network create --label "com.metahumotonic.wiki-canary.commit=$commit" --label "com.metahumotonic.wiki-canary.nonce=$rollout_nonce" "$network" >/dev/null
created_network=true
docker run -d --name "$redis_name" --network "$network" --network-alias wiki-canary-redis \
  --label "com.metahumotonic.wiki-canary.commit=$commit" --label "com.metahumotonic.wiki-canary.nonce=$rollout_nonce" "$redis_image" >/dev/null
created_redis=true
docker run -d --name "$app_name" --network "$network" --env-file "$canary_env" \
  --label "com.metahumotonic.wiki-canary.commit=$commit" --label "com.metahumotonic.wiki-canary.nonce=$rollout_nonce" -p "127.0.0.1::8000" "$image" >/dev/null
created_app=true
docker run -d --name "$gateway_name" --network "$network" --env-file "$gateway_env" --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m --memory 512m --memory-swap 512m --cpus 1 --pids-limit 128 --cap-drop ALL \
  --security-opt no-new-privileges --label "com.metahumotonic.wiki-canary.commit=$commit" --label "com.metahumotonic.wiki-canary.nonce=$rollout_nonce" \
  "$gateway_image" >/dev/null
for _attempt in $(seq 1 40); do
  if docker exec -e "MHB_GATEWAY_NAME=$gateway_name" "$app_name" python -c 'import urllib.request; urllib.request.urlopen("http://"+__import__("os").environ["MHB_GATEWAY_NAME"]+":8000/health",timeout=2)' >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec -e "MHB_GATEWAY_NAME=$gateway_name" "$app_name" python -c 'import urllib.request; urllib.request.urlopen("http://"+__import__("os").environ["MHB_GATEWAY_NAME"]+":8000/health",timeout=2)' >/dev/null
port="$(docker port "$app_name" 8000/tcp | sed -n 's/^127\.0\.0\.1://p')"
[[ "$port" =~ ^[0-9]+$ ]]

for _attempt in $(seq 1 60); do
  curl -fsS --max-time 2 "http://127.0.0.1:${port}/ready" >/dev/null && break
  sleep 1
done
curl -fsS --max-time 3 "http://127.0.0.1:${port}/ready" >/dev/null

docker exec -i -e "MHB_GATEWAY_URL=http://$gateway_name:8000" "$app_name" python - <<'PY'
import asyncio, json, os, pathlib, subprocess, sys
import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

base="http://127.0.0.1:8000"
api=base+"/api/wiki/v1"
origin="https://metahumotonic.com"
gateway=os.environ["MHB_GATEWAY_URL"]

# The TS gateway has no production datastore configuration. Every POST below
# traverses its exact legacy allowlist and lands only in this canary DB.
with httpx.Client(base_url=gateway+"/api/wiki/v1", timeout=10) as delegated:
    session=delegated.post("/sessions", headers={"Origin":origin}, json={"display_name":"Delegated Browser","actor_kind":"human"})
    assert session.status_code == 201 and session.headers.get("x-mhb-service") == "legacy-domain", session.text
    csrf=session.json()["csrf_token"]
    body={"slug":"canary-delegated-browser","title":"Delegated Browser","content":"csrf-v1","edit_summary":"canary"}
    assert delegated.post("/pages",headers={"Origin":origin},json=body).status_code == 403
    assert delegated.post("/pages",headers={"Origin":origin,"X-CSRF-Token":csrf,"Idempotency-Key":"delegated-browser"},json=body).status_code == 201
with httpx.Client(base_url=gateway+"/api/wiki/v1", timeout=10) as delegated:
    session=delegated.post("/sessions",json={"display_name":"Delegated Agent","actor_kind":"agent","agent_url":"https://metahumotonic.com/agents/canary"})
    assert session.status_code == 201 and session.headers.get("x-mhb-service") == "legacy-domain", session.text
    token=session.json()["bearer_token"]; assert token
    headers={"Authorization":f"Bearer {token}","Idempotency-Key":"delegated-create"}
    body={"slug":"canary-delegated-agent","title":"Delegated Agent","content":"first\n","edit_summary":"create"}
    first=delegated.post("/pages",headers=headers,json=body); assert first.status_code == 201, first.text
    replay=delegated.post("/pages",headers=headers,json=body); assert replay.status_code == 201 and replay.json()["replayed"] is True
    conflict=delegated.post("/pages",headers=headers,json={**body,"content":"conflict"}); assert conflict.status_code == 409
    head=delegated.get("/pages/canary-delegated-agent").json()["head_revision_id"]
    edit=delegated.post("/pages/canary-delegated-agent/revisions",headers={"Authorization":f"Bearer {token}","Idempotency-Key":"delegated-edit"},json={"expected_head_revision_id":head,"content":"second\n","edit_summary":"edit"})
    assert edit.status_code == 200
    assert delegated.post("/pages/canary-delegated-agent/revisions",headers={"Authorization":f"Bearer {token}"},json={"expected_head_revision_id":head,"content":"stale","edit_summary":"stale"}).status_code == 409
with httpx.Client(base_url=api, timeout=10) as direct:
    assert direct.get("/pages/canary-delegated-browser").status_code == 200
    assert direct.get("/pages/canary-delegated-agent").json()["content"] == "second\n"

with httpx.Client(base_url=api, timeout=10) as browser:
    session=browser.post("/sessions", headers={"Origin": origin}, json={"display_name":"Release Canary","actor_kind":"human"})
    assert session.status_code == 201, session.text
    auth=session.json(); assert not auth["bearer_token"] and auth["csrf_token"]
    body={"slug":"canary-browser","title":"Browser Canary","content":"csrf-v1","edit_summary":"canary"}
    rejected=browser.post("/pages", headers={"Origin":origin}, json=body)
    assert rejected.status_code == 403, rejected.text
    accepted=browser.post("/pages", headers={"Origin":origin,"X-CSRF-Token":auth["csrf_token"]}, json=body)
    assert accepted.status_code == 201, accepted.text

with httpx.Client(base_url=api, timeout=10) as client:
    session=client.post("/sessions", json={"display_name":"Release Agent","actor_kind":"agent","agent_url":"https://metahumotonic.com/agents/release-canary"})
    assert session.status_code == 201, session.text
    token=session.json()["bearer_token"]; assert token
    bearer={"Authorization":f"Bearer {token}","Idempotency-Key":"canary-create"}
    body={"slug":"canary-agent","title":"Agent Canary","content":"first\n","edit_summary":"create"}
    first=client.post("/pages", headers=bearer, json=body); assert first.status_code == 201, first.text
    replay=client.post("/pages", headers=bearer, json=body); assert replay.status_code == 201 and replay.json()["replayed"] is True
    conflict=client.post("/pages", headers=bearer, json={**body,"content":"conflict"}); assert conflict.status_code == 409, conflict.text
    page=client.get("/pages/canary-agent").json(); rev1=page["head_revision_id"]
    edit=client.post("/pages/canary-agent/revisions", headers={"Authorization":f"Bearer {token}","Idempotency-Key":"canary-edit"}, json={"expected_head_revision_id":rev1,"content":"second\n","edit_summary":"edit"})
    assert edit.status_code == 200, edit.text
    rev2=edit.json()["event_ids"][0]
    stale=client.post("/pages/canary-agent/revisions", headers={"Authorization":f"Bearer {token}"}, json={"expected_head_revision_id":rev1,"content":"stale","edit_summary":"stale"})
    assert stale.status_code == 409, stale.text
    history=client.get("/pages/canary-agent/history"); assert history.status_code == 200 and len(history.json()["items"]) >= 2
    diff=client.get("/pages/canary-agent/diff", params={"from_revision_id":rev1,"to_revision_id":rev2}); assert diff.status_code == 200 and "+second" in diff.json()["unified_diff"]
    assert client.get("/recent-changes").status_code == 200
    current=client.get("/pages/canary-agent").json()
    submit=client.post("/pages/canary-agent/submit-review", headers={"Authorization":f"Bearer {token}","Idempotency-Key":"canary-submit"}, json={"revision_id":current["head_revision_id"],"content_hash":current["content_hash"],"note":"release canary"}); assert submit.status_code == 202, submit.text
    report=client.post("/pages/canary-agent/report", headers={"Authorization":f"Bearer {token}","Idempotency-Key":"canary-report"}, json={"reason":"synthetic release canary"}); assert report.status_code == 202, report.text

    # The private moderation plane is exercised from inside the disposable
    # runtime only. Production Traefik never exposes this prefix.
    moderator={"X-Wiki-Moderator-Key":os.environ["MHB_WIKI_MODERATION_ADMIN_KEY"]}
    reports=httpx.get(base+"/internal/wiki/moderation/reports",headers=moderator,timeout=10)
    assert reports.status_code == 200, reports.text
    queued=next(item for item in reports.json()["items"] if item["payload"]["page_id"] == current["page_id"])
    quarantine=httpx.post(
        base+"/internal/wiki/moderation/pages/canary-agent/quarantine",
        headers={**moderator,"Idempotency-Key":"canary-quarantine"},
        json={"expected_head_revision_id":current["head_revision_id"],"content_hash":current["content_hash"],"reason":"synthetic moderation gate"},
        timeout=10,
    )
    assert quarantine.status_code == 200, quarantine.text
    assert client.get("/pages/canary-agent").status_code == 404
    assert not any(item["slug"] == "canary-agent" for item in client.get("/pages").json()["items"])
    assert client.get("/pages",params={"q":"canary-agent"}).json()["items"] == []
    assert not any(item.get("slug") == "canary-agent" for item in client.get("/recent-changes").json()["items"])
    assert client.get("/pages/canary-agent/history").status_code == 404
    assert client.get("/pages/canary-agent/diff",params={"from_revision_id":rev1,"to_revision_id":rev2}).status_code == 404
    blocked=client.post("/pages/canary-agent/revisions",headers={"Authorization":f"Bearer {token}"},json={"expected_head_revision_id":rev2,"content":"hidden edit","edit_summary":"must fail"})
    assert blocked.status_code == 404
    operator=httpx.get(base+"/internal/wiki/moderation/pages/canary-agent",headers=moderator,timeout=10)
    assert operator.status_code == 200 and operator.json()["moderation_status"] == "quarantined"
    release=httpx.post(
        base+"/internal/wiki/moderation/pages/canary-agent/release",
        headers={**moderator,"Idempotency-Key":"canary-release"},
        json={"expected_head_revision_id":current["head_revision_id"],"content_hash":current["content_hash"],"note":"synthetic release gate"},
        timeout=10,
    )
    assert release.status_code == 200, release.text
    assert client.get("/pages/canary-agent").status_code == 200
    resolved=httpx.post(base+f"/internal/wiki/moderation/reports/{queued['effect_id']}/resolve",headers=moderator,timeout=10)
    assert resolved.status_code == 200, resolved.text

config=pathlib.Path("/tmp/wiki-canary-config.json")
config.write_text(json.dumps({"base_url":base,"token":token})+"\n", encoding="utf-8"); os.chmod(config,0o600)
cli_env={**os.environ,"MHB_WIKI_CONFIG":str(config)}
def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "app.wiki_cli", *args],
        env=cli_env,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
pathlib.Path("/tmp/wiki-canary.md").write_text("cli content\n",encoding="utf-8")
cli("create","canary-cli","--title","CLI Canary","--content-file","/tmp/wiki-canary.md","--summary","canary","--idempotency-key","canary-cli-create")
cli("get","canary-cli"); cli("history","canary-cli"); cli("recent")

async def mcp_check():
    params=StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.wiki_mcp"],
        env={**cli_env,"MHB_WIKI_BASE_URL":base,"MHB_WIKI_TOKEN":token},
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools=await session.list_tools()
            names={tool.name for tool in tools.tools}
            assert names == {"wiki_get","wiki_search","wiki_create_page","wiki_create_revision","wiki_history","wiki_diff","wiki_recent","wiki_submit_for_review","wiki_report"}, names
            result=await session.call_tool("wiki_get",{"slug":"canary-cli"})
            assert result.is_error is False
asyncio.run(mcp_check())
config.unlink(missing_ok=True); pathlib.Path("/tmp/wiki-canary.md").unlink(missing_ok=True)
PY

# The application container must no longer hold a connection before the exact
# disposable database can be dropped.  The application role owns only this
# transaction-bound canary database; the validated name prevents broad drops.
cleanup
trap - EXIT
created_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python3 - "$receipt" "$commit" "$image_id" "$gateway_image_id" "$redis_image_id" "$database" "$created_at" "$migrations_sha" <<'PY'
import json, os, pathlib, sys
payload={"schema":"metahumotonic/wiki-release-canary@1","commit":sys.argv[2],"image_id":sys.argv[3],"gateway_image_id":sys.argv[4],"redis_image_id":sys.argv[5],"database":sys.argv[6],"created_at":sys.argv[7],"migrations_sha256":sys.argv[8],"tests":["ts_delegated_browser_session_csrf","ts_delegated_agent_idempotency_cas_direct_readback","browser_session_csrf","agent_bearer","idempotency_replay_conflict","create_edit_stale_cas","history_diff_recent","submit_report","moderation_quarantine_public_exclusion_release_resolve","cli_live","mcp_initialize_list_live_call"],"production_database_mutated":False,"database_cleanup":"DATA_HELPER_REQUIRED","cleanup":"PASS","status":"PASS"}
path=pathlib.Path(sys.argv[1]); tmp=path.with_suffix(".tmp")
tmp.write_text(json.dumps(payload,sort_keys=True)+"\n",encoding="utf-8"); os.chmod(tmp,0o600); tmp.replace(path)
PY
printf 'PASS disposable HTTP/CLI/MCP release canary %s\n' "$receipt"
