# 웹백의 구현·운영·은퇴 판정 그래프

2026-09-28 조사. 이 문서는 **회사 웹사이트를 지원하는 백엔드**의 구성요소를
코드 구현, 실제 운영 관측, 개발용 시뮬레이션, 은퇴 결정으로 나눈다.
[자산 카탈로그](../ts/config/platform-catalog.json)의 197개 자산·197개 관계는
프로그램과 배포의 정체성을 나타낸다. 여기의 [판정 그래프](../ts/config/backend-reality.json)는
그중 웹백에 직접 관계된 20개 구성요소와 13개 방향 관계를 별도로 평가한다.
KG의 개념 기록이나 `lifecycle: active`를 운영 정상 판정으로 사용하지 않는다.

## 확인된 현재 상태

2026-09-28 00:12–00:25 UTC [읽기 전용 운영 점검](evidence/backend-reality-2026-09-28.json)에서
VM100의 `web-back-pve-1/2`는 모두 `uvicorn app.main:app`로 실행 중이며 직접
`/health`와 `/ready`가 200이었다. Wiki 목록은 두 복제본 모두 200,
연구 요약은 모두 `source: live`였다. 공개 `/api/mcp/status`도 200이지만 등록 서버는
**0개**, 최근 검증 시각은 없다. 새 TS/Effect Dockerfile과 API는 실제 코드지만
이 Python 공개 배포를 대체한 기록이 없다. 운영 관측의 15분 유효기간이 지나면
판정 API는 자동으로 `stale`을 표시한다. 이 문서의 날짜를 현재 건강 상태로 인용하지 않는다.

| 구성 | 구현 판정 | 운영 판정 | 조치 |
|---|---|---|---|
| Python 공개 HTTP 복제본 | 실제 구현 | 00:12 UTC 운영·readiness 확인 | TS canary 전환 후 공개 ingress만 은퇴 |
| Python Wiki | 실제 도메인 소유자 | 두 복제본에서 readiness·목록 200 확인 | 유지, TS에서 비공개 위임 |
| Python ontology | 실제 도메인 소유자 | 두 복제본에서 `ontology_required=false`, `ontology_live=false` | 유지, 독립 활성화·검증 |
| Python 연구 API | 실제 구현 | 두 복제본에서 요약 200·`source: live` 확인 | 유지 |
| Python 피드백·KG proxy | 실제 구현 | 구성별 이번 직접 검증 없음 | 소유권 유지; 피드백만 TS 내구성·호환성 증명 후 경로 전환 |
| Python MCP registry | 실제 구현 | 공개 HTTP 도달 확인 | 등록 서버 검증과 TS readback 후 경로 전환 |
| 운영 MCP 등록 목록 | 조회 결과 실제 0개 | 도달하나 federation 공백 | 소유자 승인된 항목 등록·검증 |
| TS/Effect HTTP·플랫폼 그래프·학습 허브·피드백 | 실제 코드와 테스트 | 공개 트래픽 미전환 | 컨테이너·canary·공개 경로 검증 |
| TS PostgreSQL 자산·관측 저장소 | 실제 이력/receipt 코드, 임시 DB 통합 테스트 | 운영 DB 설정·수입 미완료 | 별도 DB migration/import/readback |
| TS MCP federation | 고정 바인딩·도구 허용 목록 코드 | upstream URL 미설정 | 소유 MCP와 정확한 도구 readback |
| 자산 관측 snapshot | 실제 109개 과거 증거 | 모두 만료 | 보존하되 현재 상태 판정에는 사용하지 않음 |
| 자동 probe 수집기 | 미구현 | 없음 | 고정 대상 수집·영수증·만료 계약 구현 |
| 피드백 인메모리 fallback | 개발용 시뮬레이션 | 내구성 증거 아님 | 로컬/CI용 유지 |
| DGX Kubernetes 웹백 배포·OMD driver | 이미 은퇴 | 운영 경로 아님 | 보관, 재적용 금지 |

“실제 코드”는 API와 어댑터가 구현됐다는 뜻이다. `production-observed`는 지정된
관측 시점에만 의미가 있고, 조회 시각이 `expiresAt` 이상이면 `stale`이다.
`production-unverified`는 같은 Python 프로세스 안의 기능이라도 개별 경로/저장소
판독을 이번 조사에서 증명하지 못했다는 뜻이다. `simulation`은 인메모리 동작에만
사용하며, 임시 PostgreSQL 통합 테스트를 가짜 구현으로 분류하지 않는다.

## 그래프 계약

로컬 `SYMPOSIUM/THEORY/GRAPH_ENGINEERING/PROJECT_GRAPH_ENGINEERING_BASELINE.md`의
초안 G0 기준에 맞춰 상태는 이 판정 파일, 데이터는 소유 서비스와 자산 카탈로그, effect는 기존 인증된
HTTP/MCP/DB 경계, provenance는 파일·운영 receipt로 분리한다. 이 판정 그래프는
읽기 투영이며 GraphSpec 실행성이나 GEIP D1–D4 적합성 주장이 아니다.

[Effect Schema 계약](../ts/src/domain/BackendReality.ts)이 ID 중복, dangling 관계,
존재하지 않는 플랫폼 자산 참조, 증거 없는 생산 운영 주장, 시뮬레이션의 운영 승격,
근거 없는 `SUPERSEDES` 활성화, 만료 시각과 은퇴 조건 오류를 거부한다.
관계는 `ACTIVE / PROPOSED / RETIRED`로 표시하며, 이 상태는 **코드/설계의 의미**다.
실행 권한이나 현재 트래픽의 증거가 아니다. 기존 [플랫폼 그래프](../ts/src/domain/PlatformGraph.ts)의
자산 ID를 참조하지만 다른 프로그램의 KG 정체성이나 소유권을 재작성하지 않는다.

TS 내부 `/api/platform/v1/reality`는 구성요소별 `implementation`, `runtime`,
`decision`, 시간에 따라 계산한 `evidenceState`, 다음 완료 조건을 제공한다.
`/api/platform/v1/reality/export`는 방향·타입·상태·출처를 보존한 USL
`property-graph/v2` 입력을 제공한다. `/api/platform/v1/reality/jsonld`는 inline
JSON-LD/RDF와 PROV-O 증거로 내보낸다. 관계를 `rdf:Statement`로 설명하므로
`PROPOSED` 연결을 RDF의 확정 관계로 주장하지 않는다. 세 경로 모두 기존
플랫폼 읽기 키를 요구하며 `private, no-store` 경계에 있다. 판정 파일은
플랫폼 PostgreSQL 장애 중에도 만료 표시와 함께 읽을 수 있다. MCP
`platform_reality`도 같은 판정과 `catalog/jsonld/usl` 투영을 읽는다.

독립 [SHACL 형태](../engineering/backend-reality-shapes.ttl)와
[검증기](../scripts/verify-backend-reality.py)는 named graph를 읽고 원본 ID·방향·상태,
운영 receipt, 참조 파일 존재를 대조한다. 관계 상태와 관측 만료 시각을 각각 제거한
두 반례가 SHACL에서 실패해야 통과한다. 검증 결과는 20개 구성요소·13개 관계·
8개 시점 증거, RDF 529 triples, 음성 대조 2개를 검증했다. 이것은 오프라인 출판
검증이며 새 TS 운영 배포를 뜻하지 않는다.

```sh
bash ts/scripts/with-node.sh npm --prefix ts run build
bash ts/scripts/with-node.sh node ts/scripts/export-backend-reality.mjs /tmp/backend-reality.jsonld
uv run --locked --script scripts/verify-backend-reality.py /tmp/backend-reality.jsonld
```

## 운영 전환 순서

1. 전용 플랫폼 PostgreSQL을 migration하고 현재 카탈로그 digest를 수입한다.
   기존 Wiki·제품 DB의 소유권은 유지한다. 고정 대상 probe 수집기를 만들어 실패도
   만료되는 관측으로 남기고 receipt 재전송을 검증한다.
2. 현재 비어 있는 MCP registry에 소유자 범위가 정해진 서버를 등록한다. TS의
   정해진 `mcp-bindings.json`에 주소를 설정하고 실제 도구 목록·읽기 호출·권한을
   소유 MCP와 내부 REST/MCP 양쪽에서 확인한다.
3. TS 컨테이너 통합 게이트를 실행하고 정확한 이미지 digest의 VM100 canary를
   올린다. 공개 경로, 피드백 내구성, `/ready`, 인증, Wiki/ontology의 비공개
   위임과 롤백을 같은 릴리스로 검증한다.
4. canary와 실제 트래픽 확인 뒤 Python의 **공개 진입점**만 은퇴한다. Wiki·ontology
   소유 도메인은 독립적인 대체 계약과 readback이 생길 때까지 보존한다.

현재 구현된 것은 판정 그래프, 내부 조회/JSON-LD/USL 투영, 검증 게이트다.
자동 수집기·운영 PostgreSQL 활성화·TS 트래픽 전환·MCP 등록은 아직 완료되지 않았다.

## VM100 격리 TS canary

공개 Python 복제본과 ingress를 바꾸지 않는 좁은 검증 경로는
[`ops/run-ts-backend-canary-vm100.sh`](../ops/run-ts-backend-canary-vm100.sh)다.
원격 `origin/main`에 포함된 정확한 40자리 commit만 source archive로 빌드한다.
새 TS 컨테이너는 공개 포트를 열지 않고, 기존 `web-back-pve-1`의 네트워크 namespace에서
내부 `18080`만 사용한다. 따라서 기존 Python의 `127.0.0.1:8000`을 Wiki 위임 대상으로
읽을 수 있지만 기존 컨테이너·Docker network·공개 EndpointSlice는 수정하지 않는다.

canary에는 env file이나 운영 DB/Mongo/Redis/KG/MCP 설정을 전달하지 않는다. 임시 read key로
`/ready`, Python Wiki 위임, 공개 학습 hub, platform API의 401/읽기 키, MCP initialize만 확인한다.
피드백·관측·registry·상위 MCP 쓰기 호출은 하지 않는다. 컨테이너와 이미지는 nonce 소유 label,
512 MiB/1 CPU/128 PID, read-only filesystem, no-new-privileges로 만들며 성공·실패와 관계없이
정리한다. 정리가 남았을 때만 출력된 commit·nonce로 상태 확인 또는 정리를 재실행한다.

```sh
# commit은 먼저 origin/main에 push되어 있어야 한다. 출력에는 secret이나 response body가 없다.
ops/run-ts-backend-canary-vm100.sh <COMMIT40>

# 비정상 종료 뒤 남은 동일 nonce 자원만 확인/정리한다.
ops/run-ts-backend-canary-vm100.sh --status <COMMIT40> <NONCE32>
ops/run-ts-backend-canary-vm100.sh --cleanup <COMMIT40> <NONCE32>
```

이 검증은 TS 공개 전환, 운영 PostgreSQL 활성화, Mongo 내구성 또는 upstream MCP federation의
운영 증거가 아니다. 모든 항목은 별도 canary와 rollback 검증이 필요하다.
