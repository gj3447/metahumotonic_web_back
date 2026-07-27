# metahumotonic_web_back

[metahumotonic-web](https://github.com/gj3447/metahumotonic-web) (정적 Astro 사이트)의 **백엔드 API**.
정적 사이트가 빌드 타임에 구워두던 `/api/*`를 **실시간**으로 서빙하고, 지금까지
받아주는 데가 없어 죽어 있던 **피드백 폼(`POST /api/feedback`)**을 살린다.

> Layer 분리: 이 레포 = web 백엔드 서비스. SYMPOSIUM/THEORY(논문) · bhgman_tool(7군단장 도구)과는 다른 layer.

> **개발 규율**: 이 repo는 PI 3층 개발스택(조율 OMD / 측정 ooptdd / 판정 LakatoTree) 위에서 개발한다 — [`docs/DEV_STACK.md`](docs/DEV_STACK.md). 측정층(ooptdd)은 `_vendor/ooptdd`에 vendored, 피드백 durable 게이트가 CI-enforced.

## 엔드포인트

| Method | Path | 설명 |
|---|---|---|
| GET | `/health` | 헬스체크 |
| GET | `/api/stats` | KG 통계 (nodes/rels/labels/relTypes/domains/skills) — Neo4j 실시간, 실패 시 스냅샷 |
| GET | `/api/domains` | 도메인 허브 목록 |
| GET | `/api/skills` | 스킬 목록 (7군단장 + infra/meta) |
| GET | `/api/research/summary` | 연구 본체 집계 (findings/lessons/papers/validations/consensus/decisions) + `source`(live\|snapshot) |
| GET | `/api/research/findings` | 최신 ResearchFinding (PROM 사이클 산출, `?limit&offset&cycle`) |
| GET | `/api/research/lessons` | 최신 Lesson (오답노트 wrong→truth, `?limit&offset`) |
| GET | `/api/research/papers` | Paper 코퍼스 (`?limit&offset&domain`) |
| GET | `/api/research/consensus` | PROM 사이클 합의 (`?limit`) |
| GET | `/api/research/recent` | 타입 통합 최신순 피드 (`?limit`) |
| GET | `/api/research/neighbors` | 노드의 라이브 타입별 연결(걷기) — `?name=<노드명>&limit` (cap 200, 차수 노출) |
| GET | `/api/research/agent` | AI 에이전트용 기계가독 라이브 피드 (집계 + 최신 findings/lessons) |
| POST | `/api/feedback` | 공개 피드백 접수 — 허니팟 + IP 레이트리밋 → MongoDB |
| GET | `/internal/feedback` | 내부 피드백 inbox — `X-API-Key`, public Ingress 미노출 |
| PATCH | `/internal/feedback/{id}` | 내부 triage — `reviewed|archived|spam` 상태 전이 |
| DELETE | `/internal/feedback/{id}` | 내부 영구삭제 — 연락처 포함 레코드 제거 |
| POST | `/api/kg/read` | **외부용 raw Cypher (읽기 전용)** — `X-API-Key` 게이트, Neo4j READ 트랜잭션(쓰기 서버 거부) |
| POST | `/api/kg/write` | **외부용 raw Cypher (쓰기)** — write 키만, WRITE 트랜잭션 |

모든 `/api/research/*`는 캐시(~5분) + fail-soft (KG 다운 시 빈 리스트/스냅샷, 절대 500 안 냄).

### KG Cypher 프록시 (외부 read/write 분리)

> 외부 클라이언트 연결 매뉴얼: [`docs/KG_PROXY_CONNECT.md`](docs/KG_PROXY_CONNECT.md) (키·예제·에러코드·키회전).

Community Neo4j는 RBAC가 없어서 read/write 권한 분리를 이 API 계층에서 강제한다.
키 2개를 발급하고(`MHB_KG_READ_KEY` / `MHB_KG_WRITE_KEY`, 미설정 시 503으로 비활성),
read 키는 Neo4j **READ 트랜잭션**으로 실행 → 쓰기 Cypher를 넣어도 *서버가* 거부한다
(`Writing in read access mode not allowed`). write 키는 WRITE 트랜잭션이며 `/api/kg/read`에도 통과(상위 권한).

```bash
# 읽기 (read 키)
curl -X POST https://metahumotonic.com/api/kg/read \
  -H "X-API-Key: $KG_READ_KEY" -H 'Content-Type: application/json' \
  -d '{"query":"MATCH (n) RETURN count(n) AS nodes"}'

# 쓰기 (write 키) — 파라미터는 $바인딩으로 (문자열 보간 금지, 인젝션 안전)
curl -X POST https://metahumotonic.com/api/kg/write \
  -H "X-API-Key: $KG_WRITE_KEY" -H 'Content-Type: application/json' \
  -d '{"query":"MERGE (n:Note {id:$id}) SET n.body=$body RETURN n","params":{"id":"x","body":"hi"}}'
```

응답: `{"rows":[...], "count":N, "mode":"read|write", "truncated":bool}`.
에러: 키 누락/오류 → 401, 키 미설정 → 503, KG 도달불가 → 502, 잘못된 Cypher/READ tx 쓰기 시도 → 400.
행 수는 `MHB_KG_PROXY_MAX_ROWS`(기본 1000)로 상한.

`/api/*` 응답 shape은 프론트의 `src/lib/kg.ts` / `feedback-form.js` 계약을 그대로 따른다 (drop-in).

### 피드백 계약
- body: `{ type, subject, body, email?, source_path?, contact_consent?, honeypot? }`
- `type` ∈ `general|bug|feature|thesis|compute|collaboration`
- `200 {ok, id, status}` 성공 · `200 {ok}` 봇(허니팟) 무음 처리 · `429 {reason}` 레이트리밋 · `422` 검증 실패
- 운영 환경의 `MHB_FEEDBACK_REQUIRE_DURABLE=true`에서는 MongoDB 쓰기가 확인되지 않으면 `503 storage_unavailable`을 반환하여 유실을 성공으로 위장하지 않는다.
- 원본 IP와 User-Agent는 레이트리밋 계산에만 사용하며 피드백 문서에는 저장하지 않는다.

운영자 inbox는 `MHB_FEEDBACK_ADMIN_KEY`를 설정해야만 열린다. 본문과 선택적 이메일을 포함하므로 `/internal/*`은 공개 Ingress에 연결하지 않는다. 클러스터 안에서 호출하거나 `kubectl port-forward`를 사용한다.

```sh
kubectl -n infra port-forward svc/web-back 8000:8000
curl 'http://127.0.0.1:8000/internal/feedback?limit=50' \
  -H "X-API-Key: $MHB_FEEDBACK_ADMIN_KEY"
```

## 무인프라 구동

외부 의존(Neo4j·Mongo)은 전부 **graceful degrade** — 설정 안 하면:
- `MHB_NEO4J_LIVE=false` → KG 스냅샷 fallback 값
- `MHB_NEO4J_FALLBACK_URIS=` → primary Bolt 실패 시 comma-separated backup URI 순회
- `MHB_MONGO_URI=` (빈값) → 피드백 인메모리 저장

덕분에 인프라 0으로 로컬·CI에서 그대로 돈다.

## 개발

```sh
cp .env.example .env          # 필요시 값 채우기
uv run --extra dev pytest -q  # 테스트
uv run uvicorn app.main:app --reload   # 로컬 서버 (:8000)
```

## 배포 — k8s (라이브)

bhgman 클러스터(dgx로 닿음, namespace `infra`)에 배포됨. 프론트 landing-astro와
같은 클러스터에서 Traefik IngressRoute로 `/api/*`만 이 서비스로 라우팅(additive,
기존 landing 라우트 무영향). 매니페스트: `deploy/k8s/web-back.yaml`.

배포 절차 (실측 검증된 레시피):

```sh
# 1. 레포를 dgx로 복사
rsync -az --exclude='.venv' --exclude='.git' metahumotonic_web_back/ dgx:/tmp/metahumotonic_web_back/

# 2. dgx(arm64)에서 빌드 → 인클러스터 registry push
ssh dgx '
  cd /tmp/metahumotonic_web_back
  docker build -t 192.168.0.23:30500/metahumotonic-web-back:0.8.0 .
  # docker는 NodePort registry를 insecure로 안 봄 → localhost 태그로 push (docker가 localhost는 신뢰)
  docker tag 192.168.0.23:30500/metahumotonic-web-back:0.8.0 localhost:30500/metahumotonic-web-back:0.8.0
  docker push localhost:30500/metahumotonic-web-back:0.8.0
  # kubelet은 certs.d plain-http를 안 먹음 → 이미지를 containerd k8s.io ns로 직접 import
  docker save 192.168.0.23:30500/metahumotonic-web-back:0.8.0 | sudo ctr -n k8s.io images import -
  kubectl apply -f deploy/k8s/web-back.yaml
  kubectl rollout restart deploy/web-back -n infra
'
```

> 이미지 갱신 시 태그를 올리고 위 build/import/apply 반복. 파드는
> `nodeSelector: dgx-worker`로 핀(이미지가 그 노드 containerd에 import됨).
> **새 `/api/*` prefix를 추가하면 IngressRoute match에도 그 prefix를 넣어야** 공개 도메인에서 닿는다 (경로 고정 방식).

> **2026-07-27 실측 갱신 (0.9.0):** 현재 라이브 서빙 경로는 dgx 파드가 아니라
> **VM100 (cpu-edge-01, 192.168.0.24) docker** 다 — `web-back-pve-1`(:18210) /
> `web-back-pve-2`(:18211)가 `metahumotonic-web-back:<ver>-x86` 이미지로 돌고,
> k3s `EndpointSlice`가 192.168.0.24:18210을 가리킨다 (landing의 `-pve` 패턴과 동일).
> 배포는 `bhgman` 경유 ssh로 VM100에 접속해 `~/dgx-cpu/web-back/src`를 갱신 →
> `docker build -t metahumotonic-web-back:<ver>-x86 .` → 컨테이너를 한 대씩 재생성
> (`docker inspect`로 기존 env를 그대로 추출해 재사용, 포트 18210/18211,
> `--restart unless-stopped`). 위 dgx 레시피는 구 경로 기록으로 남긴다.

## MCP 레지스트리 (`/api/mcp/*` + `mhb-mcp` CLI)

정적 `/mcp/manifest.json`을 대체하는 **라이브 MCP 레지스트리**. MongoDB
`mcp_servers` 컬렉션이 source of truth이고, HTTP는 전부 공개 읽기전용 — 쓰기는
CLI(`mhb-mcp`)로만 한다. Mongo가 죽으면 `source: "snapshot"` 빈 페이로드로
fail-soft (절대 500 없음), 정상이면 `source: "live"`. ~5분 캐시.

- `GET /api/mcp/` — 디스커버리 도큐먼트 (엔드포인트 목록 + 스키마 + 사용 예시 + 에이전트 문서 링크)
- `GET /api/mcp/servers` — 등록 서버 목록 (capabilities/auth 인리치 포함)
- `GET /api/mcp/servers/{name}` — 단일 엔트리
- `GET /api/mcp/manifest` — `metahumotonic/mcp-registry@1` 라이브 매니페스트
  (JSON-LD `@context` schema.org + 커스텀 vocab · 서버별 `capabilities`/`auth` · `credential_vault` 해금 레시피)
- `GET /api/mcp/health` — 서버별 최근 verify 결과 (status/verified_at/last_probe_at)
- `GET /api/mcp/status` — 집계 대시보드용: 서버별 배지(verified/stale/available/down/unused) +
  verified/available/down/unused/stale 카운트 + 마지막 verify 실행 시각.
  `?format=text` → 에이전트용 plain text 한 줄 요약.
  stale = 마지막 프로브가 24h 이상 지난 verified.
- `GET /api/mcp/vault` — 크리덴셜 볼트 (PBKDF2-SHA256→Fernet **암호문만**; 미초기화 시 404)
- `GET /.well-known/mcp-servers.json` — 302 → `/api/mcp/manifest` (표준 디스커버리 경로)

CLI (설치된 환경에서 `mhb-mcp`, 또는 `uv run mhb-mcp`):

```sh
mhb-mcp seed manifest.json        # Mongo에 초기 적재 (upsert, 멱등)
mhb-mcp list [--json]             # 목록 (표 또는 JSON)
mhb-mcp show <name>               # 단일 조회
mhb-mcp upsert <name> --set status=unused --set 'connection={"recipe":"http","url":"..."}'
mhb-mcp upsert <name> --file server.json
mhb-mcp remove <name>
mhb-mcp import .mcp.json          # 표준 MCP 클라이언트 설정 일괄 import (아래 참조)
mhb-mcp verify [name]             # 레시피별 프로브 → status/verified_at 기록
mhb-mcp export [--out m.json]     # Mongo → 인리치된 manifest.json (정적 폐백 갱신용)
mhb-mcp vault init --password PW [--file seed.json] [--from-mcp-json .mcp.json] \
                  [--mc-config config.json --mc-alias bhgman] [--set svc.key=v]
mhb-mcp vault unlock --password PW  # 로컬 복호화 출력 (검증용)
mhb-mcp vault show                  # 볼트 메타데이터 (KDF/서비스명만, 평문 없음)
```

### 새 MCP 서버 추가 (확장 — 하드코딩 없음)

코드 어디에도 서버 목록 하드코딩 없음. 새 서버 = CLI 한 방이면 레지스트리→
`/api/mcp/*`→`/mcp` 페이지(전부 API 렌더)까지 즉시 반영:

```sh
mhb-mcp import path/to/.mcp.json    # mcpServers 객체를 파싱해 자동 등록
# 또는 단건: mhb-mcp upsert <name> --set category=graph --set 'connection={...}'
```

import 규칙: 카테고리는 이름 기반 자동 추론(neo4j→graph, redis→vector,
minio→storage, mongo/postgres→document, 기타→utility), 레시피는 커맨드 기반
추론(ssh→`ssh-stdio`, url→`http`, 127.0.0.1:port 발견→`local-tunnel`,
npx/uvx→`local-npx`, 기타→`local-command`). 시크릿 값은 절대 저장하지 않고
`<NEO4J_PASSWORD>` 같은 플레이스홀더로 치환(URI 내장 비번도 `<MONGO_PASSWORD>`/
`<PG_PASSWORD>`/`<REDIS_PASSWORD>` 규칙). 기존 엔트리의 큐레이션 필드
(description/status/verified_at/backend/category)는 보존 — 파일은 connection의
진실, Mongo는 큐레이션의 진실.

정적 폐백 갱신(사이트 리포 커밋용):

```sh
mhb-mcp export --out ../metahumotonic-web/public/mcp/manifest.json
```

### 크리덴셜 볼트 (마스터 비번 하나로 전부 해금)

공개 표면에는 **암호문만** 존재한다. 서비스별 실제 자격증명 모음을
PBKDF2-SHA256(60만 회)으로 유도한 키의 Fernet(AES-128-CBC+HMAC)으로 암호화한
blob 하나를 Mongo에 저장하고 `GET /api/mcp/vault`가 그대로 공개한다.
에이전트 해금 절차(manifest `credential_vault`와 `/mcp/llms.txt`에도 게시):

1. 자격증명에 막히면 → 사용자에게 레지스트리 비번(6자리)을 물어본다.
2. `GET /api/mcp/vault` → `{kdf: {iterations, salt(base64)}, blob}`.
3. `key = base64url(PBKDF2-HMAC-SHA256(비번, b64decode(salt), iterations, dklen=32))`
4. `creds = json.loads(Fernet(key).decrypt(blob))` → `{service: {...}}` 전부 획득.
   CLI 검증: `mhb-mcp vault unlock --password <비번>`.

비번은 `--password` 플래그 또는 `MHB_VAULT_PASSWORD` env. **시드 JSON/평문
자격증명은 절대 커밋하지 않는다** (`--from-mcp-json`/`--mc-config`로 로컬 파일에서
직접 수집).

**로테이션 절차**:
- 레지스트리 비번 변경 → 새 비번으로 `mhb-mcp vault init` 재실행 (seed 동일).
  새 salt로 재암호화되어 blob이 원자적으로 교첐 — 구 비번은 즉시 무효.
- 서비스 자격증명 변경 → seed 갱신 후 같은 비번으로 `vault init` 재실행.
- 주기적 검증 → `mhb-mcp vault unlock`으로 전 서비스 복호화 확인.

verify 프로브: `local-tunnel` → TCP connect(127.0.0.1:port) · `http` → GET ·
`ssh-stdio` → `ssh -o BatchMode=yes <alias> true` · 로컬 실행 레시피(`local-npx`
등)는 프로브 불가 → `skip`으로 정직하게 기록. 실패 시 status=`unreachable`,
성공 시 status=`verified` + verified_at 갱신.

Mongo 대상 해석 순서: `--mongo-uri` → `MHB_MONGO_URI` env → `MONGO_PASSWORD`
env가 있으면 맥북 터널 기본값(`127.0.0.1:37017`). **시크릿은 env로만 — 코드/커밋에 넣지 않는다.**

### IngressRoute
- 둘 다 명시적 per-path prefix 매칭(전체 `/api`가 아님), priority 200:
  `PathPrefix(/api/stats | /api/domains | /api/skills | /api/research | /api/feedback | /api/kg | /api/mcp)`.
- `/.well-known/mcp-servers.json`는 별도 IngressRoute `web-back-wellknown` /
  `web-back-wellknown-tls` (priority 200 — 기존 `well-known` 라우트의 넓은
  `PathPrefix(/.well-known)`보다 긴 규칙이라 우선).
- `web-back-api` (entryPoint `web`, :80): `Host(metahumotonic.com | www | bhgman.iptime.org)`.
- `web-back-api-tls` (entryPoint `websecure`, :443, `metahumotonic-wildcard-tls`):
  `Host(metahumotonic.com | www)` — **bhgman.iptime.org 없음** (와일드카드 인증서가 그 호스트를 검증 못 함 → HTTP 전용).
- 검증: `curl https://metahumotonic.com/api/stats` → KG 통계 JSON · `curl https://metahumotonic.com/api/research/summary` → 연구 집계 JSON.

## 로컬 (Docker Compose)

```sh
docker compose up -d --build   # :8000
```

## 환경변수 (`MHB_` prefix)

`.env.example` 참조. 핵심: `MHB_NEO4J_*` (KG 읽기; `MHB_NEO4J_FALLBACK_URIS`는 comma-separated backup Bolt URI) / `MHB_MONGO_URI` (피드백 저장) /
`MHB_CORS_ORIGINS` / `MHB_FEEDBACK_MAX_PER_WINDOW` · `MHB_FEEDBACK_WINDOW_SECONDS` /
`MHB_FEEDBACK_REQUIRE_DURABLE` / `MHB_FEEDBACK_ADMIN_KEY`.

## 공개 소스와 라이선스

이 백엔드는 누구나 사용·연구·수정할 수 있도록 공개되어 있습니다.

- 저장소: [github.com/gj3447/metahumotonic_web_back](https://github.com/gj3447/metahumotonic_web_back)
- 현재 공개 릴리스의 Corresponding Source: [`v0.8.0-public.1`](https://github.com/gj3447/metahumotonic_web_back/tree/v0.8.0-public.1)
- 라이선스: [GNU AGPL v3.0 only](LICENSE)
- 벤더 코드 및 출처: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

AGPL은 수정한 백엔드를 네트워크 서비스로 운영하는 경우 그 서비스의 사용자에게
해당 수정본의 Corresponding Source를 제공하도록 요구합니다. 상업적 사용과 유료
서비스 운영도 허용되지만, 자유로운 수정·검증·재배포의 권리를 제거할 수는 없습니다.

프런트엔드 [metahumotonic-web](https://github.com/gj3447/metahumotonic-web)은
별도 저작물이며 MIT License를 유지합니다.
