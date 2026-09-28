# 회사 자산·관측 PostgreSQL 저장소

웹백은 공개 학습 허브와 회사 프로그램의 공통 API를 제공한다. 내부 자산 목록에서
프로그램·소유 저장소·서비스·MCP·배포·장비를 연결하고, 운영 관측을 출처와 함께 보존한다.
이번 단계에서는 이 관측 이력을 TS/Effect에서 PostgreSQL에 직접 저장·조회하도록 구현했다.
2026-09-28에 data-01의 전용 DB에 카탈로그 197개 자산과 과거 관측 109개를 이관했고,
런타임 계정으로 독립 readback을 확인했다. [운영 증거](evidence/company-platform-production-migration-2026-09-28.json)에
이미지·CI·DB 영수증과 권한 검증을 기록했다. 공개 요청은 여전히 Python 백엔드가 담당한다.

## 저장 모델과 역할

| 자료 | 원본과 변경 권한 | PostgreSQL 표현 |
|---|---|---|
| 프로그램·자산·관계 정의 | Git의 검토된 카탈로그, 명시적 관리자 import | `catalog_versions`와 `asset_versions`, 내용 digest별 불변 버전 |
| 수집 결과 | 플랫폼 쓰기 키를 가진 수집자 | `observations`, 추가만 허용하는 시간 이력 |
| 요청 재전송과 저장 결과 | 백엔드의 원자적 트랜잭션 | `ingest_receipts`, receipt ID와 payload digest |
| DB 구조 | 별도 migration 권한 | checksum을 보존하는 `schema_migrations` |

각 백엔드 배포는 자신의 Git 카탈로그 digest에 고정된다. 새 버전을 import해도 다른
배포의 정의를 자동 교체하지 않는다. 관측은 안정된 subject ID로 연결되므로 같은 자산을
유지한 다음 정의에서도 이전 이력을 조회할 수 있다. 제거된 자산의 과거 관측도 이력에 남는다.

조회 응답에는 `source=snapshot|postgres`, 원래 정의의 `definitionDigest`, 현재
투영의 `digest`가 있다. digest는 객체 키 순서를 정렬한 JSON 값으로 계산하므로 JSONB의
키 재배열에 영향을 받지 않는다. 파일의 SHA-256이나 USL source digest와는 다른 값이다.
PostgreSQL 모드의 현재 그래프는 subject/점검 종류별 최신 관측을 제공하고 전체 이력은
별도 API로 읽는다. “최신”은 수신 순서가 아니라 관측 시각을 기준으로 한다.

기존 Wiki PostgreSQL, 게임 상태, 수풀림 데이터, LakatoTree 원장의 소유권은 각 서비스에
있다. 회사 자산 저장소는 독립 DB·role·`mhb_platform` schema를 사용한다. Neo4j는 계속
KG를 소유하고 USL은 의미 연결을 표현한다.

## 명시적 활성화

### data-01 전용 DB 준비

기존 Wiki의 `metahumotonic_wiki`·`mhb_wiki`는 재사용하지 않는다. 전용 bootstrap은
`metahumotonic_platform`, NOLOGIN 소유 role `mhb_platform_owner`, runtime role
`mhb_platform_runtime`만 다룬다. 기본은 DB를 바꾸지 않는 dry-run이다.

```sh
ops/provision-platform-storage.sh dry-run
ops/provision-platform-storage.sh status
```

`apply`는 root-owned data-01 secret 파일에 새 runtime password와 암호화된 빈 bootstrap
baseline backup을 한 번 저장하며 stdout, Git, receipt, 명령줄에 값을 출력하지 않는다. DB/role만
만들고 migration/import나 VM100 env는 바꾸지 않는다. 빈 bootstrap만 `rollback-empty`로 보상할 수
있고, migration 또는 데이터가 있으면 중단한다. migration/import를 검증한 뒤에만 runtime grant를 실행한다.

```sh
ops/provision-platform-storage.sh apply
ops/provision-platform-storage.sh grant-runtime
```

### exact-commit migration/import (reviewed operator step)

`ops/migrate-platform-storage.sh` defaults to read-only `status`. It accepts a
digest-pinned backend image and a full Git commit only; `apply` requires a clean
checkout, verifies the image revision label, runs migration/import/readback in
an ephemeral container on data-01, and writes a root-only receipt containing
the catalog digest and row counts. The temporary migrator role and its env file
are removed before the command returns. It does not print a DSN, password, or
catalog body, and it refuses any database other than `metahumotonic_platform`.

The dedicated `metahumotonic_platform` bootstrap and catalog migration have
completed on data-01. Their root-owned receipts and encrypted baseline backup
are the operational record. The exact image ID and source archive digest are
recorded in the production evidence above. A new migration must pin and stage
its own exact commit; the registry image string below is illustrative only.

```sh
MHB_PLATFORM_IMAGE='registry.example/metahumotonic-web-back@sha256:<digest>' \
MHB_PLATFORM_COMMIT='<40-char-commit>' ops/migrate-platform-storage.sh status
# Stage the exact digest image on data-01; this does not contact PostgreSQL:
MHB_PLATFORM_IMAGE='registry.example/metahumotonic-web-back@sha256:<digest>' \
MHB_PLATFORM_COMMIT='<40-char-commit>' ops/migrate-platform-storage.sh stage-image
# After CI/review only:
MHB_PLATFORM_IMAGE='registry.example/metahumotonic-web-back@sha256:<digest>' \
MHB_PLATFORM_COMMIT='<40-char-commit>' ops/migrate-platform-storage.sh apply
```

For a local exact-commit build rather than a registry pull, first use
`ops/stage-platform-image-local.sh stage`. It archives the selected Git commit,
builds it on VM100, streams the image to data-01 with `sudo -n docker`, and
prints only the resulting image ID and source archive digest. Feed that image
ID back to the `status` and `apply` controller commands; no public ingress is
changed by either staging path.

`apply` is idempotent at the schema/catalog level. It never rolls back an
imported catalog or observations: after migration/import, the receipt records
the logical catalog digest and readback counts, then and only then grants the
limited runtime role. Any failure before that grant removes only the temporary
migrator credentials; existing platform history remains for operator review.
The receipt becomes `PASS` only after the runtime grant succeeds. A matching
`VERIFIED_BEFORE_RUNTIME_GRANT` receipt is an idempotent recovery state: rerun
the same exact image, commit, and catalog to retry only that grant. A migrator
cleanup failure is recorded as `FAILED_MIGRATOR_CLEANUP_REQUIRES_OPERATOR` and
is never reported as a successful migration.

### post-import encrypted backup

`ops/backup-platform-post-import.sh` defaults to the read-only `status` check.
Its explicit `capture` mode accepts no database name: it verifies the dedicated
database owner and the completed migration receipt, creates an AES-256 encrypted
`pg_dump` with a root-only key and receipt, then decrypts and restores it into a
temporary PostgreSQL container using the exact image ID of the running database
container. The restore container has no network and is removed before success.
The receipt becomes `VERIFIED` only after restored table counts equal the source
counts. It does not contact or back up the Wiki database.
2026-09-28에 실제 전용 DB의 암호화 백업과 동일 PostgreSQL 18 이미지에서의 격리 복원 검증을
완료했다. [백업 증거](evidence/company-platform-post-import-backup-2026-09-28.json)에
해시·수량·정리 상태를 기록했다. 이는 data-01 안의 한 번 백업이며, 외부 복제·주기 실행·
보존 정책은 아직 구성하지 않았다.

```sh
ops/backup-platform-post-import.sh status
# Explicit operator action; does not change ingress or application runtime:
ops/backup-platform-post-import.sh capture
```

기본값은 기존 Git snapshot 모드다. `MHB_PLATFORM_DATABASE_URL`을 설정하면
PostgreSQL이 필수 의존성이 된다. `MHB_PLATFORM_DATABASE_REQUIRED=true`는 URL을
빠뜨린 경우에도 readiness와 내부 자산 조회를 실패시킨다. 운영 설정에 URL이 있는데
DB가 끊긴 상황을 snapshot 성공으로 바꾸지 않는다.

HTTP 서버 시작은 migration, 카탈로그 import, 다른 데이터베이스 발견을 수행하지 않는다.
독립 회사 DB를 준비하고 관리 권한의 DSN을 환경변수로 명시한 뒤 다음을 실행한다.
도구는 `.env`를 자동 읽지 않고 연결 문자열을 로그에 남기지 않는다.

```sh
bash ts/scripts/with-node.sh npm --prefix ts run build
# 별도 회사 DB의 관리용 MHB_PLATFORM_DATABASE_URL을 secret 경로로 주입한 상태에서:
bash ts/scripts/with-node.sh node ts/scripts/platform-db.mjs migrate
bash ts/scripts/with-node.sh node ts/scripts/platform-db.mjs import
# 사용자 지정 카탈로그를 쓸 경우 import에도 정확히 같은 파일을 전달한다.
# node ts/scripts/platform-db.mjs import /path/to/catalog.json
```

두 작업은 재실행 가능하다. migration checksum이나 버전이 다르면 중단한다. import는
정의·자산·초기 관측·영수증을 하나의 트랜잭션으로 저장한다. 불완전한 import를 활성 버전으로
취급하지 않는다. 현재 기본 카탈로그의 197개 자산과 109개 과거 관측을 그대로 수입한다.

런타임은 별도의 제한된 계정을 사용한다. DBA가 생성한 `mhb_platform_runtime`에 필요한
권한 예시는 다음과 같다. 아래 문장을 운영 DB에서 자동 실행한 것은 아니다.

```sql
GRANT USAGE ON SCHEMA mhb_platform TO mhb_platform_runtime;
GRANT SELECT ON ALL TABLES IN SCHEMA mhb_platform TO mhb_platform_runtime;
GRANT INSERT ON mhb_platform.observations, mhb_platform.ingest_receipts TO mhb_platform_runtime;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA mhb_platform TO mhb_platform_runtime;
```

이 계정으로 HTTP 서버의 `MHB_PLATFORM_DATABASE_URL`을 설정한다. DDL, 정의 import,
UPDATE, DELETE 권한은 필요하지 않다. 정의·자산·영수증·관측은 SQL trigger로도 UPDATE와
DELETE를 거부한다. 장기 보존·삭제 정책은 별도 관리 migration으로 설계해야 한다.

## 수집자의 쓰기 계약

`POST /api/platform/v1/observations`는 플랫폼 **쓰기 키**만 받는다. 읽기 키는 403,
미인증 요청은 401이다. 한 요청은 관측 1–100개이며 기존 512 KiB 요청 한도를 적용한다.
주요 body 구조는 다음과 같다.

```json
{
  "receiptId": "collector:runtime:example-001",
  "observations": [{
    "id": "observation:runtime:example-001",
    "subjectId": "deployment:runtime-01:mhb-ts",
    "check": "readiness",
    "outcome": "degraded",
    "observedAt": "2026-09-27T06:17:39.003Z",
    "expiresAt": "2026-09-27T06:32:39.003Z",
    "evidence": [{
      "source": "collector:runtime:example-001",
      "authority": "SYSTEM_DERIVED",
      "observedAt": "2026-09-27T06:17:39.003Z",
      "note": "계약 설명을 위한 과거 관측 예시. 현재 운영 상태를 뜻하지 않음."
    }]
  }]
}
```

- 수집자는 자기 namespace에서 영수증과 관측 ID를 한 번 정하고 재전송에도 유지한다.
  같은 receipt ID·같은 payload는 최초 영수증과 `replayed=true`를 반환한다.
  배포의 정의 버전이 바뀌었어도 최초 영수증의 `definitionDigest`를 유지한다.
- 같은 receipt ID에 다른 내용, 또는 같은 관측 ID에 다른 내용은 409다. 전체 batch를
  rollback하므로 일부 새 관측만 저장되는 일도 없다. 같은 관측을 다른 영수증으로 재전송하면
  기존 관측을 참조하고 `insertedCount=0`으로 기록한다.
- 알 수 없는 자산, batch 안의 중복 ID, 잘못된 시간, 미래 관측, 관측 이전의 만료 시각은
  거부한다. 과거 자료 수입은 가능하고 만료된 자료는 계속 `stale`로 보인다.
- 서버가 발급하는 `receivedAt`과 수집자가 제공한 `observedAt`을 분리한다.
  기록 저장 자체가 evidence authority의 승인이나 실제 health 검증을 뜻하지 않는다.
- 요청이 끊겨 commit 여부가 불명확하면 먼저 `GET /api/platform/v1/receipts/:id`로
  확인하거나 **같은 body·같은 receipt ID**를 재전송한다. 서버는 효과를 자동 재시도하지 않는다.

변경 가능한 URL·셸 명령·DB 연결 문자열은 이 API의 실행 입력이 아니다. 기록만 추가한다.
현재 MCP에는 쓰기 수집 도구를 열지 않았으며 수집은 이 REST 경계를 사용한다.

## 읽기와 readiness

- 기존 inventory·summary·programs·graph·JSON-LD·USL API와 MCP는 같은 저장 포트를 읽는다.
- `GET /api/platform/v1/observations?subjectId=program:usl&limit=25`는 수신 이력을 최신 순으로
  조회한다. `nextBefore`를 다음 요청의 `before`로 전달하는 cursor 방식이어서 중간의 새
  수집 때문에 이미 읽은 행이 중복되지 않는다. `limit`은 최대 100이다.
- MCP `platform_observations`는 같은 이력을 제공한다. 영수증·이력도 내부 읽기 키와
  `private, no-store` 정책을 사용한다. snapshot 모드에서는 영수증·이력 API가 503이다.
- `/ready`에 `platform_postgres_required`, `platform_postgres_live`를 추가했다. 연결,
  migration checksum, 이 배포의 정의와 관측 조회가 성공해야 live다. readiness는 데이터를 쓰지 않는다.
- 필수 DB 실패는 `/ready`와 내부 자산 API에 503을 반환한다. `/health`와 공개 학습 자료는
  계속 제공한다. DB 준비 상태와 개별 프로그램의 관측 결과·만료 상태는 각각 표현한다.

## 검증과 남은 운영 작업

임시 PostgreSQL 18.6에서 통합 테스트 10개를 skip 없이 통과했다. 직접 SQL readback,
두 pool의 동시 재전송, ID 충돌 rollback, 버전 고정, cursor 이력, HTTP/MCP 일치,
DB 연결 상실 처리, 제한된 role, 실제 compiled TS 프로세스 재시작 후 보존을 확인했다.
임시 cluster를 중지하고 테스트 디렉터리와 DB를 정리했다.
[검증 기록](evidence/company-platform-postgres-2026-09-27-r2.json)을 보존한다.
일반 TS 테스트 266개와 별도 임시 Mongo/Redis 테스트 5개, 실제 서버 smoke,
Python packaging 검사 2개도 통과했다. 일반 실행에서 제외된 PostgreSQL 10개와
Mongo/Redis 5개를 각각 실제 저장소로 실행한 [통합 결과](evidence/company-platform-postgres-integration-2026-09-27.json)를 남겼다.

```sh
python3 scripts/check-platform-postgres-local.py \
  --postgres-bin /path/to/postgresql/bin \
  --receipt /path/to/new-receipt.json
```

CI에 별도 `platform-postgres` job을 추가했다. 일반 TS 테스트에서는 이 10개가 외부 DB
테스트로 제외되고, 위 실행기나 CI job에서는 누락·skip을 허용하지 않는다. 최신 원격 CI
7개 job과 data-01 이관·독립 readback은 통과했다. VM100 공개 TS 런타임의 DB 설정과
트래픽 전환은 아직 실행하지 않았다.

고정 대상 probe 수집기는 구현·테스트됐지만 운영 자동 실행은 아직 켜지 않았다. OTel,
OIDC/MCP OAuth는 후속 단계다. 기존 조사 관측은 이관되어도 자동으로 최신 상태가 되지 않는다.

트랜잭션은 같은 연결에서 수행하고 pool은 Effect Layer가 닫는다. 연결·쿼리·잠금 제한을
설정하고 SQL은 parameter binding을 사용한다. 드라이버 계약 근거:
[node-postgres transactions](https://node-postgres.com/features/transactions),
[node-postgres pool](https://node-postgres.com/apis/pool).

## PostgreSQL 전용 비공개 TS 읽기 canary

공개 전환 전에 TS/Effect 서버의 회사 자산 조회 경로만 확인하려면
`ops/provision-platform-read-canary-role.sh`와
`ops/run-ts-platform-read-canary-data01.sh`를 사용한다. 둘 다 기본값은 변경 없는
`dry-run`이며, Wiki·제품 DB, MongoDB, Redis, Neo4j, 기존 Python runtime credential을
읽거나 전달하지 않는다. 이 단계는 **운영 실행 전 CI와 ACL 검토가 끝난 operator step**이다.

전용 `mhb_platform_shadow_reader`는 `NOINHERIT`, `NOSUPERUSER`, `NOCREATEDB`,
`NOCREATEROLE`, `NOREPLICATION`, `NOBYPASSRLS` login이다. bootstrap 소유 표식과
현재 `mhb_platform` 필수 테이블이 확인된 전용 플랫폼 DB에만 생성한다. role password와
receipt는 data-01 root:root 0600 경로에만 기록하고, stdout·argv·Git·canary receipt에는
쓰지 않는다. apply 직후 SQL로 CONNECT/USAGE/현재 테이블 SELECT는 허용되고,
TEMP/CREATE/INSERT/UPDATE/DELETE/TRUNCATE는 거부되는지 확인한다. 기존 reader role,
foreign role, 불완전 플랫폼 DB, 또는 기존 secret/receipt는 fail-closed한다.

```sh
ops/provision-platform-read-canary-role.sh dry-run
ops/provision-platform-read-canary-role.sh status
# ACL review 뒤에만: ops/provision-platform-read-canary-role.sh apply
```

canary는 data-01에 이미 존재하는 tag 없는 exact image ID만 받는다. controller는 full
Git commit에서 계산한 source archive SHA-256을 image label과 비교하고,
`org.opencontainers.image.revision`도 같은 commit인지 확인한다. image publication/staging이
아직 충족되지 않으면 실행할 수 없다. 컨테이너는 PostgreSQL container의 network namespace를
공유하지만 `-p`를 사용하지 않아 공개 port가 없고, loopback HTTP probe는 `docker exec`로만
수행한다. `read-only` filesystem, tmpfs, capability drop, no-new-privileges, 512 MiB,
1 CPU, 128 pids 제한을 건다.

```sh
MHB_PLATFORM_READ_CANARY_IMAGE='sha256:<64-hex-image-id>' \
MHB_PLATFORM_READ_CANARY_COMMIT='<40-hex-commit>' \
  ops/run-ts-platform-read-canary-data01.sh dry-run
```

실행 환경에는 PostgreSQL reader DSN과 일회용 internal read key만 들어가며,
Mongo/Redis/Neo4j/legacy URI와 credential은 명시적으로 빈 값이다. Redis limiter는 GET에도
쓰기 때문에 구성하지 않는다. `MHB_SHADOW_READ_ONLY=true`는 모든 GET/HEAD/OPTIONS 이외
요청을 라우팅 전 405로 차단한다. PASS는 `/ready`의 PostgreSQL live/required, PostgreSQL
source program read, 그리고 POST 차단을 모두 뜻한다. 컨테이너와 env는 PASS/실패 뒤
정리하지만 staged image 및 reader role은 제거하지 않는다. `cleanup`은 원래 run의 `MHB_PLATFORM_READ_CANARY_NONCE`를 명시해야 하며 owner/nonce label이 일치하는 canary container와 work path만 제거한다; reader role의 `rollback-empty`는 별도 operator action이다.

rollback-empty는 DB/schema/table/sequence grant를 먼저 명시적으로 revoke하고 role drop을 확인한 뒤에만 root secret/receipt를 지운다.

새 migration은 transient migrator가 만든 table을 owner로 재소유시킬 수 있으므로 reader의 future-table 권한을 자동으로 상속하지 않는다. 새 schema version은 migration review에서 reader SELECT를 명시적으로 부여하고 privilege-denial 검증을 다시 통과한 뒤에만 이 canary 범위를 확장한다.
