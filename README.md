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
| POST | `/api/feedback` | 피드백 접수 — 허니팟 + IP 레이트리밋 → MongoDB |
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
- body: `{ type, subject, body, email?, honeypot? }` (`type` ∈ general|bug|feature)
- `200 {ok, id}` 성공 · `200 {ok}` 봇(허니팟) 무음 처리 · `429 {reason}` 레이트리밋 · `422` 검증 실패

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
  docker build -t 192.168.0.23:30500/metahumotonic-web-back:0.6.0 .
  # docker는 NodePort registry를 insecure로 안 봄 → localhost 태그로 push (docker가 localhost는 신뢰)
  docker tag 192.168.0.23:30500/metahumotonic-web-back:0.6.0 localhost:30500/metahumotonic-web-back:0.6.0
  docker push localhost:30500/metahumotonic-web-back:0.6.0
  # kubelet은 certs.d plain-http를 안 먹음 → 이미지를 containerd k8s.io ns로 직접 import
  docker save 192.168.0.23:30500/metahumotonic-web-back:0.6.0 | sudo ctr -n k8s.io images import -
  kubectl apply -f deploy/k8s/web-back.yaml
  kubectl rollout restart deploy/web-back -n infra
'
```

> 이미지 갱신 시 태그를 올리고(예 0.6.1) 위 build/import/apply 반복. 파드는
> `nodeSelector: dgx-worker`로 핀(이미지가 그 노드 containerd에 import됨).
> **새 `/api/*` prefix를 추가하면 IngressRoute match에도 그 prefix를 넣어야** 공개 도메인에서 닿는다 (경로 고정 방식).

### IngressRoute
- 둘 다 명시적 per-path prefix 매칭(전체 `/api`가 아님), priority 200:
  `PathPrefix(/api/stats | /api/domains | /api/skills | /api/research | /api/feedback | /api/kg)`.
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
`MHB_CORS_ORIGINS` / `MHB_FEEDBACK_MAX_PER_WINDOW` · `MHB_FEEDBACK_WINDOW_SECONDS`.
