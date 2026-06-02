# metahumotonic_web_back

[metahumotonic-web](https://github.com/gj3447/metahumotonic-web) (정적 Astro 사이트)의 **백엔드 API**.
정적 사이트가 빌드 타임에 구워두던 `/api/*`를 **실시간**으로 서빙하고, 지금까지
받아주는 데가 없어 죽어 있던 **피드백 폼(`POST /api/feedback`)**을 살린다.

> Layer 분리: 이 레포 = web 백엔드 서비스. SYMPOSIUM/THEORY(논문) · bhgman_tool(7군단장 도구)과는 다른 layer.

## 엔드포인트

| Method | Path | 설명 |
|---|---|---|
| GET | `/health` | 헬스체크 |
| GET | `/api/stats` | KG 통계 (nodes/rels/labels/relTypes/domains/skills) — Neo4j 실시간, 실패 시 스냅샷 |
| GET | `/api/domains` | 도메인 허브 목록 |
| GET | `/api/skills` | 스킬 목록 (7군단장 + infra/meta) |
| POST | `/api/feedback` | 피드백 접수 — 허니팟 + IP 레이트리밋 → MongoDB |

`/api/*` 응답 shape은 프론트의 `src/lib/kg.ts` / `feedback-form.js` 계약을 그대로 따른다 (drop-in).

### 피드백 계약
- body: `{ type, subject, body, email?, honeypot? }` (`type` ∈ general|bug|feature)
- `200 {ok, id}` 성공 · `200 {ok}` 봇(허니팟) 무음 처리 · `429 {reason}` 레이트리밋 · `422` 검증 실패

## 무인프라 구동

외부 의존(Neo4j·Mongo)은 전부 **graceful degrade** — 설정 안 하면:
- `MHB_NEO4J_LIVE=false` → KG 스냅샷 fallback 값
- `MHB_MONGO_URI=` (빈값) → 피드백 인메모리 저장

덕분에 인프라 0으로 로컬·CI에서 그대로 돈다.

## 개발

```sh
cp .env.example .env          # 필요시 값 채우기
uv run --extra dev pytest -q  # 테스트
uv run uvicorn app.main:app --reload   # 로컬 서버 (:8000)
```

## 배포

```sh
docker compose up -d --build   # :8000
```

프론트(nginx) 또는 Traefik에서 `/api/*` → 이 서비스로 프록시.
예: `api.metahumotonic.com` → `web-back:8000`, 또는 프론트 nginx에
`location /api/ { proxy_pass http://web-back:8000; }`.

## 환경변수 (`MHB_` prefix)

`.env.example` 참조. 핵심: `MHB_NEO4J_*` (KG 읽기) / `MHB_MONGO_URI` (피드백 저장) /
`MHB_CORS_ORIGINS` / `MHB_FEEDBACK_MAX_PER_WINDOW` · `MHB_FEEDBACK_WINDOW_SECONDS`.
