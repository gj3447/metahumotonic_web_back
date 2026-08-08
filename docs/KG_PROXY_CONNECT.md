# KG Cypher 프록시 — 외부 연결 매뉴얼

metahumotonic Knowledge Graph(Neo4j)에 **외부에서 read/write 권한을 나눠** 접속하는 방법.

- **Base URL**: `https://metahumotonic.com/api/kg`
- **인증**: HTTP 헤더 `X-API-Key`
- **요청/응답**: JSON (`Content-Type: application/json`)
- **메서드**: `POST` (read 조회도 POST — 본문에 Cypher를 담기 때문)

---

## 1. 키 두 개

| 키 | 쓸 수 있는 엔드포인트 | 권한 |
|---|---|---|
| **READ 키** | `/api/kg/read` | 읽기 전용 |
| **WRITE 키** | `/api/kg/read` + `/api/kg/write` | 읽기 + 쓰기 (상위 권한) |

> 왜 read 키로는 쓰기가 안 되나: 이 서버의 Neo4j는 Community 에디션이라 DB 자체 권한분리(RBAC)가 없다. 대신 read 요청은 **Neo4j READ 트랜잭션**으로 실행돼서, 쓰기 Cypher를 넣어도 *Neo4j 서버가* 거부한다 (`Writing in read access mode not allowed`). 즉 키워드 필터가 아니라 DB 레벨에서 진짜로 막힌다.

키는 코드/문서에 박지 말고 환경변수로 관리:

```bash
export KG_READ_KEY='<read 키>'
export KG_WRITE_KEY='<write 키>'
```

---

## 2. 엔드포인트

### `POST /api/kg/read` — 읽기
- 헤더: `X-API-Key: <READ 키 또는 WRITE 키>`
- READ 트랜잭션. 쓰기 Cypher → 400.

### `POST /api/kg/write` — 쓰기
- 헤더: `X-API-Key: <WRITE 키만>`
- WRITE 트랜잭션. `CREATE`/`MERGE`/`SET`/`DELETE` 가능.

### 요청 본문

```json
{
  "query": "MATCH (n:Lesson {name:$name}) RETURN n.problem AS problem",
  "params": { "name": "lesson-xxx" }
}
```

- `query` (필수): Cypher 문자열. 1~20000자.
- `params` (선택): 바인딩 파라미터. **값은 반드시 `params`로 넘기고 쿼리 안에서는 `$이름`으로 참조** — 문자열로 직접 이어붙이지 말 것 (인젝션 안전 + 캐시 효율).

### 응답 본문

```json
{
  "rows": [ { "problem": "..." } ],
  "count": 1,
  "mode": "read",
  "truncated": false
}
```

- `rows`: 결과 행 배열 (Neo4j 타입은 JSON 스칼라로 변환됨).
- `count`: 행 수.
- `mode`: `"read"` | `"write"`.
- `truncated`: `true`면 상한(기본 1000행)에서 잘렸다는 뜻 → `LIMIT`/`SKIP`으로 페이지네이션.

---

## 3. 예제

### curl

```bash
# 읽기 — 전체 노드 수
curl -s -X POST https://metahumotonic.com/api/kg/read \
  -H "X-API-Key: $KG_READ_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"query":"MATCH (n) RETURN count(n) AS nodes"}'

# 읽기 — 파라미터 바인딩
curl -s -X POST https://metahumotonic.com/api/kg/read \
  -H "X-API-Key: $KG_READ_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"query":"MATCH (l:Lesson) WHERE l.name=$n RETURN l.problem AS p","params":{"n":"lesson-xxx"}}'

# 쓰기 — MERGE (write 키 필요)
curl -s -X POST https://metahumotonic.com/api/kg/write \
  -H "X-API-Key: $KG_WRITE_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"query":"MERGE (n:Note {id:$id}) SET n.body=$body RETURN n.id AS id","params":{"id":"x1","body":"hello"}}'
```

### Python

```python
import os, requests

BASE = "https://metahumotonic.com/api/kg"

def kg_read(query, params=None):
    r = requests.post(f"{BASE}/read",
        headers={"X-API-Key": os.environ["KG_READ_KEY"]},
        json={"query": query, "params": params or {}}, timeout=30)
    r.raise_for_status()
    return r.json()["rows"]

def kg_write(query, params=None):
    r = requests.post(f"{BASE}/write",
        headers={"X-API-Key": os.environ["KG_WRITE_KEY"]},
        json={"query": query, "params": params or {}}, timeout=30)
    r.raise_for_status()
    return r.json()["rows"]

print(kg_read("MATCH (n) RETURN count(n) AS nodes"))
kg_write("MERGE (n:Note {id:$id}) SET n.body=$b RETURN n",
         {"id": "x1", "b": "hello"})
```

### JavaScript (fetch)

```javascript
async function kgRead(query, params = {}) {
  const res = await fetch("https://metahumotonic.com/api/kg/read", {
    method: "POST",
    headers: {
      "X-API-Key": process.env.KG_READ_KEY,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ query, params }),
  });
  if (!res.ok) throw new Error(`${res.status}: ${(await res.json()).detail}`);
  return (await res.json()).rows;
}

await kgRead("MATCH (n) RETURN count(n) AS nodes");
```

---

## 4. 에러 코드

| HTTP | `detail` | 의미 | 대처 |
|---|---|---|---|
| **200** | — | 성공 | — |
| **400** | `Writing in read access mode not allowed …` | read 엔드포인트에 쓰기 Cypher | `/api/kg/write` + write 키 사용 |
| **400** | Neo4j 구문 에러 메시지 | 잘못된 Cypher | 쿼리 수정 |
| **401** | `invalid or missing X-API-Key` | 키 없음/틀림, 또는 write에 read 키 | 올바른 키 |
| **422** | (FastAPI 검증) | 본문 형식 오류 (`query` 누락 등) | 본문 점검 |
| **502** | `KG unreachable: …` | Neo4j 도달 불가 | 잠시 후 재시도 |
| **503** | `kg proxy disabled (no key set)` | 서버에 키 미설정 (프록시 비활성) | 운영자 문의 |

---

## 5. 한도 / 주의

- **행 상한**: 응답 기본 1000행 (`MHB_KG_PROXY_MAX_ROWS`). `truncated:true`면 `SKIP`/`LIMIT`으로 나눠 가져올 것.
- **쿼리 타임아웃**: 서버측 약 10초. 무거운 전체 스캔은 피하고 라벨/인덱스를 활용.
- **파라미터화 필수**: 사용자 입력은 항상 `params`로. 쿼리 문자열 연결 금지.
- **read 키는 진짜 읽기 전용**: read 키가 유출돼도 그래프를 변경할 수 없다 (Neo4j READ tx). write 키는 더 조심해서 보관.
- **CORS**: 브라우저에서 직접 호출은 `metahumotonic.com` 출처만 허용. 서버-사이드/스크립트에서 호출 권장.

---

## 6. 키 회전 (운영자용)

라이브 백엔드는 Kubernetes Deployment가 아니라 VM100의 Docker 컨테이너 두 개다.
따라서 Kubernetes Secret을 patch하거나 `deployment/web-back`을 restart해도 라이브 키는
바뀌지 않는다.

키 회전은 승인된 비노출 운영 경로로 VM100의 root-owned 환경 파일을 갱신하고, 두
컨테이너를 **한 대씩** 재생성한다. 각 컨테이너의 직접 `/health`와 `/ready`가 통과한 뒤
다음 replica를 교체하고, 마지막에 `ops/check-web-back-live.sh`로 공개 readback까지
검증한다. 키 평문을 Git, 명령행 인자, 셸 기록, 로그 또는 Docker inspect 영수증에 남기지
않는다. 정확한 불변조건과 롤백 조건은 [`OPERATIONS_VM100.md`](OPERATIONS_VM100.md)를
따른다.

키를 비우면(unset) 해당 엔드포인트는 503으로 비활성화된다 (안전 기본값).

---

## 7. 빠른 점검 (배포 검증)

```bash
# 읽기 OK
curl -s -o /dev/null -w "read:  %{http_code}\n" -X POST https://metahumotonic.com/api/kg/read \
  -H "X-API-Key: $KG_READ_KEY" -H 'Content-Type: application/json' -d '{"query":"RETURN 1"}'           # 200

# read 키로 쓰기 → 막힘
curl -s -o /dev/null -w "rdwr:  %{http_code}\n" -X POST https://metahumotonic.com/api/kg/read \
  -H "X-API-Key: $KG_READ_KEY" -H 'Content-Type: application/json' -d '{"query":"CREATE (n) RETURN n"}' # 400

# 키 없이 → 거부
curl -s -o /dev/null -w "nokey: %{http_code}\n" -X POST https://metahumotonic.com/api/kg/read \
  -H 'Content-Type: application/json' -d '{"query":"RETURN 1"}'                                          # 401
```

---

*구현: `app/routers/kg_proxy.py` (라우터) + `app/kg.py` `KGClient.run_cypher` (READ/WRITE tx 강제). 운영 정본: `docs/OPERATIONS_VM100.md`. 설계 근거: KG `dl-web-back-kg-proxy-community-no-rbac-app-layer-enforcement-2026-06-23`.*
