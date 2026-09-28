# 공개 학습 허브와 의미 연결

홈에서 선의 공리·세계관·공개 헌장·프로그램·GitHub·YouTube를 이어 읽는다.
프런트엔드는 자매 저장소 `metahumotonic-web`의 `/`와 `/learn/`에 구현했다.
기존 정전의 문장을 수정하지 않고 읽기 경로와 출처 안내를 추가했다.

## 하나의 공개 원본, 세 가지 표현

- 원본: `ts/config/learning-hub.json`. 공개 확인된 19개 항목, 23개 방향 관계,
  네 가지 입문 경로. 회사 내부 프로그램 카탈로그와 별개다.
- 검증과 투영: `ts/src/domain/LearningHub.ts`. Effect Schema가 비공개 항목,
  알 수 없는 필드, 잘못된 URL, 중복·끊어진 ID를 거부한다.
- 웹: 같은 투영을 `metahumotonic-web/src/data/learning-hub.json`에 고정해
  정적 렌더링한다. 방문 시 KG, GitHub, YouTube를 조회하거나 플레이어를 자동 로드하지 않는다.
- JSON-LD: 안정된 HTTPS IRI, Schema.org의 DefinedTerm/LearningResource/
  SoftwareSourceCode/VideoObject, RDF 관계 진술, PROV 출처.
- USL: 같은 IRI를 native UID로 사용하는 `property-graph/v2` 입력.
  관계 방향·상태·출처·의미 설명이 포함된다. resolver 허용 범위는 이 파일에서 파생하지 않는다.

REST는 `/api/public/v1/hub`, `/api/public/v1/hub/graph.jsonld`,
`/api/public/v1/hub/usl.json`이다. 키 없이 공개 원본만 반환한다.
기존 `/api/platform/v1/*`와 `/mcp` 인증은 유지한다. 인증된 MCP에는
`platform_learning` 도구를 추가했고 `catalog`, `jsonld`, `usl` 형식을 제공한다.

정적 웹은 `/learn/data.json`, `/learn/graph.jsonld`, `/learn/usl.json`을 제공한다.
HTML에 JSON-LD를 포함하고 `rel=alternate`로 독립 문서를 발견할 수 있게 했다.
Nginx에는 `application/ld+json`을 명시했다. API·HTML·USL은 같은 `sourceDigest`를 가진다.

```sh
bash ts/scripts/with-node.sh npm --prefix ts run build
bash ts/scripts/with-node.sh node ts/scripts/export-learning-hub.mjs \
  /path/to/metahumotonic-web/src/data/learning-hub.json
# 원본과 프런트엔드의 생성 파일이 정확히 일치하는지 확인
bash ts/scripts/with-node.sh node ts/scripts/export-learning-hub.mjs \
  /path/to/metahumotonic-web/src/data/learning-hub.json --check
```

프런트엔드 재빌드와 두 저장소의 배포가 필요하다. 공개 원본을 변경했다고 운영
홈페이지가 자동 갱신되지는 않는다. 내부 KG 레코드를 자동 공개하는 작업도 없다.

## 의미의 범위

`DEFINES`, `DOCUMENTED_IN`, `SOURCE_CODE`, `EXPLORE_NEXT`, `PUBLISHES`를 사용한다.
이 로컬 어휘의 정의는 `/learn/#relation-…`에 공개된다. 특히 `EXPLORE_NEXT`는
편집자의 읽기 추천이며 동일성·인과·실행 의존성 주장이 아니다. PROPOSED/RETIRED는
ACTIVE 의미 관계로 내보내지 않는다. RDF 진술과 USL 설명에는 원래 상태를 남긴다.

기존 `ontology.ttl`은 별도 소유의 정전 투영이다. 예전 세계관의 `HSWM` 사도 역할과
현재 소프트웨어를 임의 `owl:sameAs`로 합치지 않았다. 이 허브는 표준 형식으로
연결 가능한 공개 읽기 그래프이며 GEIP 인증·전면 추론 엔진·자동 동기화 주장은 아니다.

## 공개 출처 확인

2026-09-27에 YouTube `@metahumotonic`의 실제 HTML에서 `(주)메타휴모토닉`,
채널 ID `UCLVZA8cxVCEqrREfZ7NgcJA`, 선의 공리가 담긴 설명을 확인했다.
`/videos`의 실제 게시 목록에서 세 영상을 선택했다:

- HSWM 인류보완계획 — `smwfb9vYG10`
- 스페이스 걸 — `jnN1_dmvpns`
- 그라데이션 하늘 — `GhFKWh3T6zE`

이는 제목·게시 채널 확인이다. 전사·내용 검증·연구 효능 증거가 아니다.
GitHub의 HSWM, lakatotree, metahumotonic-foundation은 인증 없는 공식 API에서
HTTP 200과 `private=false`를 확인했다. 원문 정의는 기존 사이트의 `axioms.json`,
사명과 자유의 정의는 `foundation.json`의 공개 내용을 근거로 했다.
USL·HSPINE의 같은 소유자명 GitHub 후보는 인증 없는 조회에서 404였으므로 공개
코드 링크를 만들지 않았다. 저장소가 없다고 단정하지 않고 기존 공개 소개로 연결한다.

## 검증

백엔드 테스트는 공개/비공개 API 분리, 입력 거부, JSON-LD와 USL의 관계 보존,
공식 MCP 클라이언트의 `platform_learning` 호출을 검사한다. 프런트엔드는 생성
파일 digest·실제 HTML 앵커·로컬 링크·미디어 링크와 자동 재생 부재를 검사한다.

실제 USL 어댑터의 19개 노드·23개 관계 변환과 RDFLib JSON-LD 1.1 Dataset의
442개 triple 및 23개 관계 진술을 확인했다. named graph로 읽어야 하며 단일 기본
graph만 검사해서 빈 결과를 전체 결과로 취급하지 않는다.

브라우저 검증은 `metahumotonic-web`의 `npm run test:workbench:browser`에 포함된다.
이는 로컬 검증이며 운영 게시·검색엔진 인덱싱의 증거가 아니다.

형식 근거: [W3C JSON-LD 1.1](https://www.w3.org/TR/json-ld11/),
[Schema.org LearningResource](https://schema.org/LearningResource).

## 1단계: 의미 계약과 독립 readback

G0의 읽기 그래프로 시작한다. 의미 연결을 실행 스케줄러나 서비스 호출 권한으로
해석하지 않는다. `DEFINES`는 원문→개념, `DOCUMENTED_IN`은 개념/프로젝트→원문,
`SOURCE_CODE`는 프로젝트/원문→저장소, `PUBLISHES`는 채널→영상으로 제한한다.
편집 추천인 `EXPLORE_NEXT`에는 의미 있는 순환을 허용한다. 자기 연결·중복 경로·
중복 출처·실재하지 않는 날짜·발행일 이후 검토일·종류에 맞지 않는 주소는 거부한다.

프런트엔드 빌드 마지막에 `dist/learn/publication.json`을 생성한다. 홈, 학습 화면,
catalog, JSON-LD, USL 다섯 파일의 실제 바이트 해시와 공개 원본 digest를 담으며
PASS 판정이나 생성 시각은 넣지 않는다. 별도 Python 검증기가 파일을 다시 읽어
원본, ID, 방향, 의미, 상태, 출처, 읽기 순서, 실제 HTML 앵커를 대조한다.

```sh
# 백엔드 빌드 → 프런트엔드 생성본 일치 확인 → 웹 빌드 → 독립 검증
bash ops/verify-company-hub.sh ../metahumotonic-web
# 새 측정 기록이 필요하면 두 번째 인자로 미존재 파일 경로를 지정한다.
# 기존 기록은 덮어쓰지 않는다.
```

`engineering/learning-hub-phase-1.prediction.json`은 이번 측정 전에 작성한 로컬
예측이며 LakatoTree 소유자 사전등록이 아니다. 자동 생성된
`docs/evidence/learning-hub-phase-1-publication-2026-09-27.json`은 검증기 해시,
측정 시각, 다섯 파일 해시를 포함한 **로컬 산출물** readback 기록이다.
운영 게시·서명·GEIP D1–D4 인증·LakatoTree 판결로 승격하지 않는다.

웹 배포 스크립트는 활성화 전에 산출물을 검증하고, 활성화 후에는 독립적으로 보유한
해시 목록과 실제 origin 응답을 대조한다. HTTP 200이어도 오래된 본문이면 실패한다.
실패 시 이전 공개본의 해시로 복구를 검증하며 성공 전에는 배포 상태를 전진시키지 않는다.
현재 허브가 있는데 manifest가 사라졌으면 옛 홈페이지 검사로 우회하지 않는다.
구체적인 운영 설치 순서는 프런트엔드 `docs/LEARNING_HUB_2026-09-27.md`에 있다.

다음 단계는 승인된 실제 MCP/server binding을 서비스별로 연결해 소유 서비스에서
결과를 읽어 검증하고, 컨테이너와 stage origin을 검증하는 것이다. 이번 단계에서는
운영 서비스를 연결하거나 ingress를 전환하지 않았다.

## 후속: 회사 웹의 프로젝트·사도·공리 구조와 실제 공개 배포

2026-09-27, 사용자 요청으로 공개 그래프를 **51개 항목·79개 관계·6개 읽기 경로**로
확장했다. 기존 안정 ID를 유지하고 프로젝트 9개, 사도 12개, 공리의 열두 정의,
철학과 Ultra Safety AI/Agent 설명을 공개 읽기 그래프로 연결했다. 사도는 `apostle`
종류의 DefinedTerm이며 사람/직원/소프트웨어로 분류하지 않는다. INTRODUCES는
글에서 개념·사도·프로젝트로 향하는 소개 관계이며 Schema.org about으로 투영한다.

공리의 PRIMARY_SOURCE summary는 원문 그대로이며 SKOS definition으로 투영한다.
사도와 Agent 소개는 EDITORIAL_SUMMARY다. Agent는 기존 Ultra Safety AI 사명의
실행 관점이며 공식 명칭 변경이나 구현 완료를 뜻하지 않는다. `publicUrl`은 추가한
공개 경로와 1–12 공리 앵커만 허용하고 임의 fragment·API 경로는 계속 거부한다.

`engineering/learning-hub-shapes.ttl`과 `scripts/verify-learning-rdf.py`가 named RDF
1275 triples, 출처/상태의 필수 구조와 반례를 검증했다. 실제 USL 어댑터의 51/79 변환도
`ts/scripts/check-learning-usl.mjs`로 확인했다. 두 증거는 각각
`docs/evidence/learning-rdf-2026-09-27-r2.json`, `learning-usl-2026-09-27-r2.json`에 있다.
learning-hub 및 platform HTTP/MCP 테스트 37개가 통과했다.

정적 웹은 13:47 UTC에 기존 VM100 서빙 경로로 실제 배포했다. 공개 HTTPS 45개 파일과
JSON-LD MIME, 의미 관계의 독립 readback이 통과했다. 프런트엔드의
`docs/SEMANTIC_COMPANY_WEBSITE_2026-09-27.md`에 릴리스 해시·이전 버전·복구 경로가 있다.
백엔드 HTTP/MCP 운영 서버는 이번 웹 배포로 전환하지 않았다. API 변경은 로컬 코드와
검증된 배포 후보이고, 공개 웹은 이를 정적으로 투영한 버전을 사용한다.
