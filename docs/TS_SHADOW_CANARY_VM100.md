# VM100 TS/Effect 실의존성 shadow canary

`ops/run-ts-shadow-canary-vm100.sh <exact-commit>`는 TS 후보를 기존 Python
`web-back-pve-1`의 network namespace 안에서만 일시 실행한다. ingress, EndpointSlice,
기존 Python container, Wiki·Mongo·Redis·Neo4j·PostgreSQL 데이터를 변경하지 않는다.

호출 전에는 exact commit이 `origin/main`에 있어야 한다. 이 도구는 root-owned mode 0600
`/etc/metahumotonic/web-back.env`에서 Neo4j read, Mongo registry read, Redis ping, 선택적
platform PostgreSQL read 설정만 Docker에 스트림으로 전달한다. Wiki DB/session secret,
feedback admin key, KG/platform write key, MCP upstream token은 전달하지 않는다. 값은
stdout, receipt, 임시 env file에 기록하지 않는다.

```sh
ops/run-ts-shadow-canary-vm100.sh --dry-run <40-char-commit>
ops/run-ts-shadow-canary-vm100.sh <40-char-commit>
```

candidate는 `MHB_SHADOW_READ_ONLY=true`로 시작한다. outer HTTP boundary가 GET/HEAD/OPTIONS
이외 요청을 405로 거부하고, feedback store도 TTL index 생성과 모든 write를 거부한다.
canary는 live KG research, Mongo MCP registry count, Redis/Mongo readiness, Python Wiki와
ontology delegation status, 그리고 configured platform PostgreSQL의 readback을 확인한다.
platform PG가 없으면 그 사실을 receipt에 구분해 남긴다.

성공 후에도 container/image/workdir를 nonce ownership 검사 후 자동 삭제한다. cleanup이
필요하면 `ops/run-ts-shadow-canary-vm100.sh --cleanup <commit> <nonce>`를 사용한다. 이
canary가 통과해도 public traffic 전환이나 Python owner retirement를 뜻하지 않는다.
