# Python → TS 공개 API parity gate

`scripts/check-api-parity.py`는 Python 운영 후보와 TS/Effect 후보의 읽기 전용 GET
응답을 비교하는 pre-ingress gate다. 실제 운영 URL을 추정하지 않는다. 호출자가 두 private
origin을 명시적으로 주입한다.

```sh
python3 scripts/check-api-parity.py \
  --python-origin http://127.0.0.1:18110 \
  --ts-origin http://127.0.0.1:18180 \
  --receipt /secure/release-receipts/api-parity.json
```

비교 대상은 코드의 고정 allowlist뿐이다. 모든 요청은 `GET`이고, redirect를 따라가지
않으며, 응답은 1 MiB로 제한한다. 비교하는 표면은 기존 KG·research·MCP registry와
well-known redirect, 존재하지 않는 경로의 404다. Wiki·ontology·feedback·KG write와
플랫폼 관측 쓰기는 의도적으로 포함하지 않는다. 각각 별도 상태 전이 또는 인증 parity
gate가 필요하다.

JSON은 key order를 canonicalize한다. 실행마다 달라질 수 있는 다음 필드만 명시적으로
제외한다: `request_id`, `instance`, `deployedAt`, `generated_at`, `evaluatedAt`, `updated`,
`last_verify_at`. 그 밖의 차이는 실패다. `research-summary`는 양쪽 모두 `source=live`여야
한다. status와 `content-type`, `cache-control`, `etag`, `x-data-source`,
`x-data-quality`, `x-records-omitted`도 비교한다.

receipt에는 origin URL, 인증 값, 응답 본문, response header 값이 들어가지 않는다. 성공한
canonical JSON 비교는 digest로만 남긴다. 이 gate는 actual ingress 전환이나 rollback을
수행하지 않는다.
