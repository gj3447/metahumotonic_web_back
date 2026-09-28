# 회사 프로그램과 데이터 소유권

최신 범위는 **40개 프로그램·197개 자산·197개 관계**다.
[회사 플랫폼 설계](COMPANY_PLATFORM_ARCHITECTURE.md)에 전체 환경 조사 후의 확장과
기술 결정을 정리했다. 아래 내용은 처음 강조된 다섯 프로그램과 PostgreSQL 소유권의 근거다.

2026-09-27 사용자 지적에 따라 프로그램 범위를 보완했다. 앞선 요약은 기반 도구와
Mongo·Redis 검증에 치우쳤고 메이플리니지를 빠뜨렸다. 회사 공통 백엔드는 제품과
연구 프로그램을 함께 등록하고 연결하는 진입점이다.

| 프로그램 | 확인한 역할과 구현 | 저장소·서비스 연결 범위 |
|---|---|---|
| 버엑시 | 버츄얼 엑셀방송 시뮬레이터. 독립 `virtual-excel-simulator` 저장소, 실행 루트 `GAMES/the-excel-tycoon`, 설계·연구 그래프 | 게임 원본과 상태 소유권은 제품에 둔다. DB 종류나 운영 서버 연결은 이번 이름 확인으로 추정하지 않는다. 기존 `program:virtual-excel` ID를 유지하고 한국어 이름·별칭을 추가했다. |
| 수풀림 | 방송 생태계의 공개 근거 그래프와 커뮤니티. 독립 `soopoolim`, TS/Effect | 기본은 분리된 SQLite. community·LiveRead·publication·일부 근거 원장에 선택적 PostgreSQL 어댑터가 있다. 공개 근거·커뮤니티·신원 vault의 분리를 보존한다. |
| 메이플리니지 | 독립 `MAPLELINEAGE` 게임과 권위 있는 게임 백엔드, 개발 자료 MCP | KG에는 PostgreSQL 18.3의 개발 DB·역할·migration 확인 기록이 있다. 게임 서비스의 거래와 게임 상태는 소유 서버가 처리한다. 개발 MCP는 고정 corpus 읽기만 제공한다. |
| USL | KG·URL·Git·파일시스템 사이 의미 연결 | 원본 ID·방향·상태·출처를 보존한다. 회사 카탈로그를 실제 `property-graph/v2` 어댑터에 통과시켰다. |
| HSWM | 지속적 토큰 기반 월드모델·실행 셀 연구와 개발 런타임 | 프로그램 정체성, 개발 도구 사용, 실증 연구 효능과 운영 가동 상태를 각각 기록한다. 제품의 게임 상태나 사용자 의도를 회사 카탈로그가 대신 소유하지 않는다. |

다섯 항목은 이번 사용자가 직접 강조한 범위다. 회사 전체의 우선순위를 KG가
자동 판정했다는 뜻은 아니다. 기존 HSPINE·Graph Engineering·MCP Connectors·
Agent Coding Paradigm 등도 유지해 이 보완 당시 카탈로그는 **15개 프로그램, 46개 노드,
28개 관계**였다. `lifecycle=active`는 개발 프로그램 상태이며 운영 health 판정이 아니다.

후속 [KG·작업환경 전체 점검](WORKSPACE_KG_AUDIT_2026-09-27.md)에서 이 15개가
회사 전체 목록은 아님을 확인했다. 추가 제품·운영 저장소, 기존 제품의 실제 배포,
Python 운영본과 별도 TS runtime의 상태는 해당 관측과 증거를 따른다.

## PostgreSQL도 기존 운영 인프라다

KG에는 범용 PostgreSQL과 LakatoTree 원장 PostgreSQL이 각각 ACTIVE 인프라로 등록돼
있다. 기존 회사 Wiki도 PostgreSQL event/revision/CAS/idempotency 저장소를 사용하며,
`service:legacy-domains → datastore:postgresql` 관계가 이미 카탈로그에 있었다.
메이플리니지의 PostgreSQL 개발 DB 연결 근거와 관계를 이번에 추가했다.

동일한 PostgreSQL 인프라를 사용하더라도 Wiki·게임·수풀림·LakatoTree의 논리 DB,
계정, migration, 데이터 소유권은 별도다. 회사 백엔드의 Mongo는 피드백·MCP 레지스트리,
Redis는 제한 상태, Neo4j는 KG를 담당한다. PostgreSQL을 제외한 DB 구성으로 전환한
것이 아니다. TS는 기존 Wiki 소유 서비스의 `/ready`와 `wiki_store_live`를 통해
도메인 저장소 준비 상태를 전달한다.

KG 기록 재조회와 실제 저장소 README/HEAD 확인은 현재 운영 DB에 SQL을 실행한
가동 확인과 구분한다. 특히 메이플리니지의 개발 DB 생성 기록을 플레이어 대상
운영 배포 완료로 확대하지 않는다.

이번 보완에서는 로컬 PostgreSQL 18.6으로 별도 임시 cluster를 만들어 기존 Wiki
저장소의 실DB 통합 테스트 **7개를 skip 없이 통과**시켰다. migration의 idempotency와
drift 거부, 저장 결과의 독립 SQL 조회, 동시 CAS, 명령 replay/충돌, 보존 정책 등을
검사하고 cluster와 임시 파일을 정리했다. 운영 DB에는 연결하지 않았다.
[실행·정리 기록](evidence/company-wiki-postgres-2026-09-27-r2.json)을 보존했으며,
CI에도 별도 `wiki-postgres` job을 추가했다. CI의 PostgreSQL 이미지는 18.3으로
고정했지만 원격 CI 실행은 아직 하지 않았다.

```sh
python3 scripts/check-wiki-postgres-local.py \
  --postgres-bin /path/to/postgresql/bin \
  --receipt /path/to/new-receipt.json
```

## MCP 연결 상태

Ontology와 HSPINE은 별도 임시 백엔드를 통해 실제 읽기 호출을 검증했다.
메이플리니지 개발 MCP는 소유 README에 선언된 세 도구만 읽기 허용 목록에 추가했다:
`maplelineage_status`, `maplelineage_search_design`, `maplelineage_verify_harness`.

`MHB_MAPLELINEAGE_MCP_URL`과 `MHB_MAPLELINEAGE_MCP_TOKEN`이 비어 있으면
`configured=false`다. 회사 백엔드의 연결 edge는 PROPOSED로 유지한다. status 도구는
고정 개발 자료의 상태이며 게임 서버나 PostgreSQL의 health가 아니다. 이 MCP가
게임 DB, 정전 원문, 임의 파일, 셸 명령을 실행할 수 있다고 해석하지 않는다.

후속 전체 점검에서는 VM100의 기존 `maplelineage-dev-mcp` 컨테이너가
running / healthy인 것을 확인했다. 기존 서버 가동과 새 회사 백엔드의 연결 설정은
서로 다른 상태다. 수풀림·버엑시의 기존 운영 컨테이너도 별도로 확인했다.

각 제품의 runtime API·사용자 세션·게임 권한 연결은 소유 서비스의 계약에 따라
별도 검증한다. 이번 보완은 내부 인증된 프로그램 카탈로그에 적용했으며,
공개 학습 허브에는 검토된 공개 소개만 싣는 기존 절차를 유지한다.

근거: [KG·저장소 관측](evidence/program-inventory-correction-2026-09-27.json),
[갱신한 USL 변환 기록](evidence/usl-program-inventory-2026-09-27.json),
[카탈로그](../ts/config/platform-catalog.json), [MCP 허용 목록](../ts/config/mcp-bindings.json).
