# OMD 병렬 개발 파이프라인 — web_back

> **조율(coordination) 층**. N개 Claude 세션이 이 repo를 **동시에** 편집할 때, 서버가
> 서로소(disjoint) write-set lease + git worktree 격리로 충돌을 *사전* 차단하고 CLOUD
> CONNECT로 되머지한다. OMD는 정의물이 아니다 — 시작 전 작성할 스키마가 없다.
> 3층 스택 전체: [`DEV_STACK.md`](DEV_STACK.md). 상위 정본: `delltower_import/CLAUDE.md`.

## 작업큐 (라이브 오빗, coord db 등록됨 2026-07-13)

각 태스크의 write-set은 서로소 → 4개 에이전트가 동시에 물어도 충돌 없음. `pyproject.toml`
등 hot 파일은 `shared` 레인(git 3-way 응결). `next(agent)`가 우선순위대로 READY를 준다.

| task_id | 내용 | write-set (배타) | shared |
|---|---|---|---|
| `web_back/omd-1-ooptdd-oo-prod` | ooptdd OpenObserve 프로덕션 backend + 게이트 확장 | `app/trace.py` · `gates/*.yaml` · `tests/test_ooptdd_*.py` | `pyproject.toml` |
| `web_back/omd-2-kg-proxy-audit` | KG proxy per-query 감사로그 + row-cap/READ-tx 테스트 | `app/routers/kg_proxy.py` · `tests/test_kg_proxy.py` | `pyproject.toml` |
| `web_back/omd-3-research-neighbors-perf` | research neighbors 페이지네이션/성능 + 캐시키 경계 | `app/routers/research.py` · `tests/test_research_endpoints.py` | `app/kg.py` |
| `web_back/omd-4-observability-metrics` | Prometheus 라벨 정비 + observability 테스트 | `app/observability.py` · `tests/test_observability.py` | `pyproject.toml` |

## 드라이버 — happy-path 2-verb

에이전트당 부팅 시 `begin` 한 번(→declare 게이트+claim+promote+start 원샷, worktree 자동
격리), 마감 시 `complete_task` 한 번(→commit+finish+connect+push). 충돌은 fail-loud.

```
# 에이전트 부팅 — 작업큐에서 하나 집어 격리 진입
mcp__omd__begin        agent=<나> task=web_back/omd-2-kg-proxy-audit \
                       writes=["web_back/app/routers/kg_proxy.py","web_back/tests/test_kg_proxy.py"]
# … 격리된 worktree 안에서만 편집 + 로컬 pytest …
mcp__omd__heartbeat    task=…                 # 긴 작업이면 주기적으로(liveness)
mcp__omd__complete_task task=web_back/omd-2-kg-proxy-audit msg="feat(kg-proxy): per-query audit log"
```

관측/회복:
- `mcp__omd__next     agent=<나>` — 지금 안전한 서로소 READY 추천.
- `mcp__omd__task_conditions task=…` — deps_satisfied/held/heartbeat_fresh/merge_ready (read-only).
- `mcp__omd__status` — 전체 오빗/태스크. `sweep`=만료 GC, `bail`=비상탈출, `cancel`=미시작 종결.

## 규율 (모든 세션 공통)

1. **편집 전 lease** (`begin` 또는 `declare`+`claim` HELD 확인) — 예외 없음. 다른 세션의
   미커밋 파일과 겹치면 손 떼고 분리 태스크로.
2. **커밋은 pathspec** (`git commit -- <내 파일들>`) — 인덱스 스윕 금지. 커밋 후 즉시 push.
3. 새 병렬 작업은 서로소 write-set으로 `declare`해서 이 큐에 추가.

<!-- KG: project_metahumotonic_web_integrate_core_dev_tech_2026_07_13, project_omd_develop_queue_2026_07_13 -->
