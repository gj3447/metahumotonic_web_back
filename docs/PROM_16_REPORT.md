# PROM 16 — metahumotonic_web_back 백엔드 구현/발전 리서치

> `/prom 16` (4 axis × 4 sub-axis = 16 병렬 haiku 리서치) · cycle `prom16-webback-impl-2026-06-02`
> Lesson: `lesson-webback-impl-research-2026-06-02` · 16/16 ResearchFinding 수확

축: **A1 앱 아키텍처** · **A2 KG/데이터 접근** · **A3 보안/abuse** · **A4 배포/관측성/계약**
렌즈: **S1 공식** · **S2 함정** · **S3 대안** · **S4 트렌드2026**

---

## 0. 사전 지식 — 현재 백엔드가 이미 맞게 하는 것

리서치가 검증한, 지금 구현이 **이미 best-practice인** 부분 (바꾸지 말 것):

- **FastAPI + uvicorn 유지가 정답** (A1S3). 이 워크로드는 I/O-bound(Neo4j/Mongo 왕복이 지배) → 프레임워크 직렬화 속도(Litestar/Go/Rust)는 무의미. 재작성 비용만 발생.
- **AsyncGraphDatabase + lifespan 싱글톤 드라이버** (A1S1, A2S1) — 이미 `kg.py`가 lazy 싱글톤 async 드라이버, `main.py` lifespan에서 `kg.close()/store.close()`. 정석.
- **graceful degrade** (A2S2) — Neo4j/Mongo 불가 시 스냅샷/인메모리 폴백. 권장 패턴 그대로.
- **pydantic-settings 환경변수 + 허니팟 필드** (A1S1, A3S1) — 기본 방어 OK.

→ 즉 **아키텍처 토대는 옳다.** 아래는 "작동" → "프로덕션급"으로 가는 갭.

---

## 1. Consensus (3+ 셀이 동의 = 높은 신뢰)

### C1. `count(n)` 전체 스캔을 매 요청 → **캐시 필수** (A2S1·A2S2·A2S3·A2S4)
현재 `/api/stats`는 `MATCH (n) ... count(n)` + `MATCH ()-[r]->() count(r)`를 매 요청 실행 = 90k 노드/614k 관계 **전체 그래프 스캔**. "거의 안 변하는 통계"엔 과부하.
- **권고**: (a) 짧은 TTL 캐시(예 60–300s), 또는 (b) `CALL apoc.meta.stats()` / `db.stats` 같은 메타 카운트(전체 스캔 회피), 또는 (c) materialized `:Stats` 노드 + 주기 갱신.
- 단일 replica면 **in-process TTL**(cachetools/aiocache memory)로 충분. 다중 replica로 가면 **Redis**(C2와 공유).

### C2. in-memory 레이트리밋은 다중 replica/재시작에서 무력 → **Redis 분산 레이트리밋** (A3S1·A3S2·A3S3·A3S4 만장일치)
현재 `ratelimit.py` = 프로세스 메모리 슬라이딩 윈도우. replica 2개면 한도 2배로 새고, 파드 재시작이면 리셋. **이게 보안 갭 1순위.**
- **권고**: 인클러스터 **Redis(data ns)** 백엔드 슬라이딩 윈도우(Redis Lua atomic) 또는 `slowapi`+Redis. 단일 replica인 지금도 "재시작 리셋"은 남으므로 Redis 권장.

### C3. graceful shutdown 미설정 → 롤아웃/종료 시 요청 끊김 (A1S1·A1S2·A4S1·A4S2)
lifespan은 있으나 k8s 레벨 `preStop` + `terminationGracePeriodSeconds` + uvicorn SIGTERM 처리 부재.
- **권고**: Deployment에 `lifecycle.preStop: sleep 5–10s` + `terminationGracePeriodSeconds: 60` + uvicorn graceful timeout.

### C4. 단일 replica + nodeSelector 핀 = SPOF (A4S1·A4S2·A4S3)
파드/노드 죽으면 다운, 노드 장애 시 스케줄 불가. 근본 원인 = 이미지가 dgx-worker containerd에만 `ctr import`됨(레지스트리 pull 실패).
- **권고(연쇄)**: ① `/etc/containerd/certs.d/<registry>/hosts.toml` plain-http 정상화 → kubelet이 레지스트리 pull 가능 → ② `ctr import`+nodeSelector 핀 제거 → ③ **replicas 2 + topologySpreadConstraints**로 HA. (단 containerd 재시작은 GPU 노드 영향 주의 → OQ1)

### C5. 헬스 프로브 분리: liveness vs readiness (A4S1·A4S2)
현재 `/health` 하나로 liveness+readiness 겸용. 의존성(Neo4j/Mongo) 체크가 liveness에 섞이면 DB 일시 장애에 파드 재시작 루프.
- **권고**: `/health`(liveness=프로세스 200) + `/ready`(readiness=의존성 graceful 체크, 실패해도 폴백되므로 degraded 표시). graceful degrade라 readiness는 "항상 200 + degraded 플래그"가 안전.

### C6. 관측성 부재 → OpenTelemetry 3-signal + 구조화 로깅 (A4S1·A4S4)
- **권고**: `opentelemetry-instrumentation-fastapi` + Prometheus 메트릭(`/metrics`) + JSON 구조화 로그(structlog) + request/trace ID. OTel은 2026/05 CNCF 졸업 = 사실상 표준.

### C7. 프론트-백 계약을 OpenAPI→타입 클라이언트로 (A1S4·A4S4)
프론트가 지금 `/api/stats`를 손으로 fetch(방금 만든 `kg-live.js`). FastAPI가 OpenAPI 자동 생성하므로 → `openapi-typescript`로 타입 안전 클라이언트 생성 가능.
- **권고**: P2. `openapi-typescript`로 프론트에 타입 생성, contract drift 차단(Schemathesis CI).

---

## 2. Divergence (셀 간 갈림 — 판단 필요)

| 주제 | 입장 A | 입장 B | 해소 |
|---|---|---|---|
| **stats 캐시 위치** | in-process TTL(단순, 단일 replica) | Redis/materialized 노드(다중 replica) | **C2가 Redis를 이미 요구** → stats도 Redis 캐시로 통일 (또는 단기엔 in-process TTL + 후속 Redis) |
| **레이트리밋 계층** | 앱(slowapi+Redis) | 엣지(Traefik/Cloudflare Turnstile) | **레이어드** — 엣지(Turnstile, cloudflared 이미 있음)로 봇 차단 + 앱 Redis로 정밀 한도 |
| **HA 방식** | 다중 replica(레지스트리 fix 필요) | 단일 replica + 다운타임 수용(개인 사이트) | 사용자 가용성 요구에 의존 → **OQ2** |
| **배포 파이프라인** | 최소(containerd fix만) | GitOps(Flux/ArgoCD) | 단일 앱 규모 → containerd fix + GHA push, GitOps는 앱 늘면 |

---

## 3. Open Questions (사용자 verdict 필요)

- **OQ1**: containerd `certs.d` 정상화는 **GPU 프로덕션 노드 containerd 재시작**을 수반. 지금 감수할지, 아니면 `ctr import` 핀 유지할지? (재시작 = 잠깐 파드 영향)
- **OQ2**: 이 백엔드의 가용성 목표는? "개인 사이트, 가끔 죽어도 됨"이면 단일 replica 유지가 합리(과설계 회피). "항상 떠야"면 C4 HA 추진.
- **OQ3**: 봇 방어를 Cloudflare Turnstile(프론트 위젯 + 백 검증)까지 갈지, 허니팟+Redis 레이트리밋으로 충분히 볼지? (피드백 스팸 실제 발생량에 의존 — 지금은 0)

---

## 4. 권장 후속 작업 (우선순위)

### P0 — 싼데 효과 큰 것 (지금 바로)
1. **stats 캐시** (C1): `/api/stats` 60–300s TTL 캐시. count 전체 스캔 → 메타 카운트(`apoc.meta.stats` 가능 시) 또는 캐시. *가장 큰 부하 제거.*
2. **Mongo 피드백 TTL/cap** (A3S2): `web_feedback`에 TTL 인덱스(예 365d) 또는 크기 제한 — 무한 적재 방지.
3. **XFF 신뢰 교정** (A3S2): `_client_key`가 `X-Forwarded-For` 첫 헤더를 무조건 신뢰 → Traefik이 붙인 신뢰 프록시 값만. 스푸핑으로 타인 한도 소진 차단.

### P1 — 프로덕션 견고성
4. **Redis 분산 레이트리밋** (C2): 인클러스터 Redis 백엔드. 재시작/다중 replica 안전.
5. **graceful shutdown** (C3): preStop + terminationGracePeriodSeconds + uvicorn SIGTERM.
6. **헬스 분리** (C5): `/health`(live) + `/ready`(degraded-aware).
7. **관측성** (C6): OTel FastAPI instrumentation + `/metrics` + JSON 로그.

### P2 — 성숙화
8. **HA** (C4, OQ1/OQ2 해소 후): containerd certs.d fix → 핀 제거 → replicas 2 + spread.
9. **타입 클라이언트 계약** (C7): `openapi-typescript` 프론트 생성 + Schemathesis contract test CI.
10. **엣지 봇 방어** (OQ3): Cloudflare Turnstile(이미 cloudflared 경유).

### 안 해도 되는 것 (과설계 회피)
- 프레임워크 교체(Litestar/Go/Rust) ✗ — I/O-bound라 무의미 (A1S3).
- Python 3.13 free-threading ✗ — I/O-bound엔 GIL 제거 이득 없음 (A1S4).
- GraphQL/Harbor/ArgoCD ✗ — 단일 앱 규모엔 과함 (A4S3).

---

## 구현 상태 (2026-06-02)

**구현·배포 완료** (v0.2.0 → v0.4.0):
- C1 stats TTL 캐시(120s) · Mongo 피드백 TTL(365d) · XFF 앱-측 교정(cf-connecting-ip/rightmost)
- C2 Redis 분산 레이트리밋(sorted-set 슬라이딩윈도우, graceful 폴백)
- C3 graceful shutdown(preStop+graceTerm60) · C5 헬스 분리(/health·/ready)
- C6 관측성: Prometheus `/metrics` + structlog (uvicorn 액세스로그 JSON화는 minor 후속)
- C4 HA: replicas 2 + PDB(minAvailable1) + RollingUpdate(maxUnavailable0) — same-node
- Turnstile 백엔드 검증 scaffold(env-gated, `MHB_TURNSTILE_SECRET` 설정 시 활성)
- Dockerfile `pip install .`(pyproject) — 의존성 드리프트 방지

**의도적 보류** (정직 공시 — 위험/가치 부적합):
- **IP-attribution (Traefik forwardedHeaders)**: Traefik이 Helm 관리 + replicas 1 + Recreate → 변경 시 사이트 전체 ingress 수초 다운 + Helm 드리프트. 0-스팸 개인 피드백폼엔 부적합. **정확한 fix**: Helm values `ports.web.forwardedHeaders.trustedIPs`(cloudflared/pod CIDR + Cloudflare 대역) — Helm으로 persist되게. 앱은 이미 cf-connecting-ip 우선이라 Traefik이 넘기는 순간 per-IP 자동 동작.
- **true multi-node HA / containerd certs.d fix (OQ1)**: dgx-worker에 vLLM(GPU) 가동 중 → containerd 재시작 위험. same-node replicas 2로 대체(pod-crash·rollout 무중단 확보, node-failure는 SPOF로 수용=OQ2).
- **Turnstile 활성화 (OQ3)**: Cloudflare 대시보드 site key 필요(외부 인증). scaffold만 ready.
- **C7 타입 클라이언트**: 프론트가 plain-JS 정적 사이트라 가치 대비 과함. 필요 시 openapi-typescript로 즉시 가능.

## 부록: 16셀 출처 요약

| 셀 | 한줄 |
|---|---|
| A1S1 | lifespan 싱글톤 드라이버 + Depends 주입 + k8s는 replica로 수평확장(워커 X) |
| A1S2 | async 내 blocking 호출이 이벤트루프 정지 = 최악 함정; lifespan 리소스 정리 |
| A1S3 | FastAPI+uvicorn 유지가 정답(I/O-bound, 직렬화 무의미); Granian은 CPU 포화 측정 시만 |
| A1S4 | FastAPI 최신+uv+Pydantic v2 strict; 3.13 free-thread는 I/O엔 무익 |
| A2S1 | 드라이버 1회 생성+pool 재사용, execute_query, routing='r', 파라미터 바인딩 |
| A2S2 | 요청마다 드라이버 생성=pool 고갈; execute_read lazy 스트리밍(메모리 100배); 캐시 |
| A2S3 | 거의 안 변하는 통계 = materialized 노드 또는 TTL 캐시; Redis는 변화율 높을 때 |
| A2S4 | GraphRAG/vector+graph 하이브리드, read replica, ontology 서빙(이미 함) |
| A3S1 | OWASP API Top10; in-mem 레이트리밋 클라우드서 무효; Mongo operator 화이트리스트 |
| A3S2 | XFF 스푸핑; 허니팟≠차단; Mongo TTL; CORS 명시; generic 에러 |
| A3S3 | 4계층(Traefik 엣지+Redis+허니팟+점진 challenge); slowapi+Redis |
| A3S4 | Turnstile AI스크래퍼 차단, OIDC/mTLS, External Secrets, Cosign 이미지 서명 |
| A4S1 | RollingUpdate(maxUnavailable0/maxSurge1), liveness/readiness 분리, PDB, OTel |
| A4S2 | 단일replica+nodeSelector=SPOF; certs.d plain-http fix; preStop+graceTerm; CORS 충돌 |
| A4S3 | containerd insecure fix(2h)→Flux 최소 GitOps; cron-pull은 GitOps 아님 |
| A4S4 | OpenAPI→TS 클라이언트, Astro 하이브리드(딱 맞음), OTel CNCF 졸업, contract test |
