# TS/Effect 공개 백엔드 전환 게이트

2026-09-28 기준 metahumotonic.com의 공개 API owner는 Python이다. TS/Effect는
격리 canary에서 회사 플랫폼 PostgreSQL의 197개 자산·109개 과거 관측을 읽고,
쓰기 요청을 405로 차단하는 단계까지 검증했다. 이 결과는 공개 트래픽 전환이나 전체
Python 기능 대체를 뜻하지 않는다. [운영 증거](evidence/company-private-ts-platform-read-canary-2026-09-28.json)를
기준으로 다음 경계를 차례로 닫는다.

Wiki·ontology의 고정 익명 GET/HEAD 위임은 [VM100 private canary](evidence/company-private-wiki-delegation-read-canary-2026-09-28.json)에서
Python 직접 응답과 일치했다. 이 조회는 Python의 Redis 읽기 제한 카운터를 올릴 수
있다. 이 조회만으로 세션·변경 요청·장애 동작은 검증되지 않는다.

Wiki의 긍정 경로 상태 변경도 [별도 private canary](evidence/company-private-wiki-stateful-delegation-canary-2026-09-28.json)에서
통과했다. TS를 거친 브라우저 세션·쿠키·CSRF 쓰기와 agent bearer·멱등 재전송·
충돌·stale revision CAS를 복원한 일회용 DB에 실행하고 Python 직접 읽기로 확인했다.
Python의 moderation·CLI·MCP 기준 경로도 같은 일회용 런타임에서 통과했고 DB는
삭제했다. 이어서 [반복 가능한 private drill](evidence/company-private-wiki-stateful-drill-redis-recovery-2026-09-28.json)에서
검증된 백업 영수증부터 일회용 DB 생성·삭제까지 자동화하고, 격리 Redis 중단 시
세션·조회·쓰기의 Python/TS 503 응답과 재시작 후 복구를 확인했다. Redis 블랙홀의
시간 제한, 전체 경로 응답 동등성, 운영 저장소 권한은 여전히 별도 검증 대상이다.

Python rate limiter에는 Redis 연결·명령·종료의 3초 제한과 동시 요청의 재연결
폭주 방지가 적용됐다. 모의 블랙홀·동시 요청 테스트와 CI를 통과한 코드는
[Python 공개 릴리스](evidence/company-python-redis-timeout-rollout-2026-09-28.json)에서
두 복제본에 배포되어 정상 조회를 확인했다. 실제 네트워크 블랙홀의 종단 지연은
아직 측정하지 않았다.

| 순서 | 전환 조건 | 완료 판정 |
|---|---|---|
| 1. Wiki·ontology 위임 | Python은 Wiki DB·세션·ontology의 private owner로 유지한다. TS의 고정 경로 위임에서 익명/세션 발급, 쿠키·CSRF 변경, agent bearer 변경, idempotency 재전송, ETag 충돌, moderation의 public 404·내부 접근, Redis 장애를 실제 두 런타임으로 비교한다. | GET뿐 아니라 상태 변경·장애 계약이 일치하고 Python 데이터 소유권이 유지된다. |
| 2. 상태 저장소 권한 | Mongo feedback/MCP용 목적별 계정과 Redis limiter용 좁은 ACL을 마련하고, 실제 인덱스·TTL·쓰기 내구성·재시작 readback을 검증한다. Neo4j Community에는 DB 수준 reader RBAC가 없으므로 검증된 별도 읽기 투영이나 제한된 gateway 없이는 native KG live를 공개하지 않는다. | 각 계정의 실제 ACL과 실패 시 동작이 영수증으로 검증된다. |
| 3. API 범위 | 공개 학습 허브와 PostgreSQL 자산 읽기는 먼저 옮길 수 있다. sanitized MCP directory는 실제 Mongo readback 뒤, platform observation POST와 MCP upstream 호출은 각각 별도 쓰기 키·receipt·allowlist 검증 뒤에 연다. | [API parity gate](API_PARITY_GATE.md)의 제외 범위를 해소하고 공개/비공개 권한 경계가 유지된다. |
| 4. 이중 런타임 배포 | TS immutable image·root-only 설정·독립 readiness·실제 public GET parity 검사·이전 ingress 상태 저장·원자적 전환·즉시 복귀를 하나의 release controller로 묶는다. Python replicas와 Wiki owner는 복귀 기간 동안 유지한다. | TS 전용 release/rollback을 실환경에서 왕복 검증하고 공개 라우팅을 점진적으로 변경한다. |
| 5. 관측 후 은퇴 | 에러·지연·인증 실패·데이터 보존을 정해진 관측 기간 동안 비교한다. | Python의 **공개 ingress/deployment**만 은퇴한다. Wiki·ontology·KG의 private owner와 복귀 이미지는 별도 전환 증거가 생길 때까지 유지한다. |

현재 [Python release controller](../ops/release-web-back-vm100.sh)는
`Dockerfile.legacy`와 Python replicas를 다루며 TS의 공개 배포·복귀 도구가 아니다.
[TS 위임 포트](../ts/src/ports/LegacyService.ts)는 허용된 Wiki·ontology 경로만
전달한다. [Mongo feedback](../ts/src/ports/FeedbackStoreMongo.ts)은 실제 쓰기와
인덱스 생성이 있고, [Redis limiter](../ts/src/ports/RedisLimiter.ts)는 읽기 요청에서도
Lua로 상태를 변경한다. 그러므로 PostgreSQL 전용 canary의 PASS를 이 저장소들의
무쓰기 검증으로 확대하지 않는다.

USL·JSON-LD·SHACL 그래프 표현과 PostgreSQL의 버전 고정 카탈로그는 회사 자산의
의미 연결에 사용한다. 런타임 health는 별도 시각·출처·만료가 있는 관측으로만 표현하며,
이관한 109개 과거 관측을 현재 정상 상태로 취급하지 않는다.
