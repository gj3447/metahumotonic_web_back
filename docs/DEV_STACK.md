# 개발 스택 — PI 3층 규율 (조율 · 측정 · 판정)

> 이 repo(그리고 자매 [`metahumotonic-web`](https://github.com/gj3447/metahumotonic-web))는
> 사용자 자작 3층 개발기술 위에서 개발한다. 상위 정본:
> `SYMPOSIUM/GIT/delltower_import/CLAUDE.md`.

| 층 | 도구 | 이 repo에서의 역할 |
|---|---|---|
| **조율** | **OMD** (`mcp__omd__*`) | 여러 Claude 세션이 이 repo를 병렬 편집할 때 write-set을 사전 lease → 충돌 사전방지 |
| **측정** | **ooptdd / LTDD** | 실행 트레이스가 ground truth. 반환값·`200 OK` 자기보고를 믿지 않고, store를 읽어 *실제로 일어난 일*을 positive assert |
| **판정** | **LakatoTree** | 사전등록 예측 대비 실측으로 진보/퇴행 판정 (손입력 verdict 금지). airo KG 트리 `LakatosTree_MetahumotonicWebStack_20260713` |

---

## 측정 (ooptdd) — 배선됨 ✅

`ooptdd`는 `_vendor/ooptdd/`에 vendored(34파일, self-contained, PyPI/네트워크 불필요).
드리프트는 `_vendor/test_ooptdd_vendor_drift.py`가 지킨다(정본과 sha256 대조).

**핵심 사례 — 피드백 durable 저장.** `store.save()`는 Mongo가 조용히 죽어
in-memory로 fallback해도 `record_id`를 반환하고 엔드포인트는 `200 {ok,id}`를 준다.
반환값 테스트는 green-and-blind. 측정층은 트레이스를 읽어 durable 저장을 assert한다:

- `store.save()` → `feedback_received`(항상) + `feedback_durably_stored`(Mongo insert 성공 시에만) emit (`app/trace.py`).
- Red spec: [`gates/feedback_durability.yaml`](../gates/feedback_durability.yaml).
- 게이트 테스트: `tests/test_ooptdd_feedback_gate.py` — durable=GREEN / silent-loss=RED(엔드포인트는 여전히 200).

```sh
uv run --extra dev pytest tests/test_ooptdd_feedback_gate.py -v   # 게이트 3종
uv run --extra dev pytest -q                                       # 전체(게이트+드리프트 포함)
```

게이트 테스트는 기존 pytest CI 잡(`.github/workflows/ci.yml`)에 자동 포함 → **CI-enforced**.

**프로덕션 트레이스 → OpenObserve.** 기본은 zero-infra in-memory backend.
`MHB_OOPTDD_OO_URL`(+`OOPTDD_OO_PASSWORD`) 설정 시 OpenObserve로 ship(dgx 내부망 :5080).
새 파이프라인 이벤트를 추가할 땐 `trace.emit(...)` + gate YAML 한 줄.

## 판정 (LakatoTree) — 라이브 ✅

airo KG 트리 `LakatosTree_MetahumotonicWebStack_20260713`(assurance_tier=notebook).
비자명한 변경은 **먼저 예측을 사전등록**하고, 측정 스크립트 결과를 제출해 자동 판결받는다
(LLM 점수/손입력 금지). 첫 판결: ooptdd durability 게이트 → **progressive** (delta 1.0, novel).

```
mcp__lakatotree__add_node          <tree> <tag> …          # frontier 노드
mcp__lakatotree__register_prediction <tree> <tag> metric baseline novel_threshold credence closes_question
# … 실험 실행 (pytest 등) …
mcp__lakatotree__submit_result     <tree> <tag> value script novel_measured   # → progressive/partial/rejected
```

CI(GitHub 호스티드)는 ZeroTier airo KG에 못 닿으므로 판정층은 **로컬/에이전트 tier**
(MCP 경유)로 돈다. 값소유·판정은 airo KG 박스(딜타워/Mac serve)에서.

## 조율 (OMD) — 라이브 작업큐 ✅

**서로소 오빗 4개가 coord db에 등록됨** → N 세션 동시 편집 안전. 백로그·드라이버(2-verb
`begin`/`complete_task`)·규율 정본: [`OMD_PARALLEL.md`](OMD_PARALLEL.md).

여러 세션이 이 repo를 병렬 편집하면 편집 *전* write-set을 lease한다(예외 없음):

```
mcp__omd__declare  task=<작업> writes=[<파일globs>] deps=[…]   # orbit 등록(disjoint)
mcp__omd__next     agent=<나>                                   # 안전한 READY 태스크
mcp__omd__start    task=<작업> agent=<나>                        # worktree 기동
mcp__omd__claim    agent=<나> paths=[…] task=<작업>             # HELD 확인 후에만 편집
# … 편집(내 worktree 안에서만) …
mcp__omd__commit   task=<작업> msg=…;  mcp__omd__finish task=<작업>
mcp__omd__connect  task=<작업>                                   # CLOUD CONNECT = 실제 git 머지(fenced)
```

- 다른 세션의 미커밋 파일과 겹치면 손 떼고 분리 태스크로.
- 커밋은 pathspec(`git commit -- <내 파일들>`) — 인덱스 스윕 금지. 커밋 후 즉시 push.
- 단일 세션 개발이면 lease는 no-op이지만, 규율은 동일하게 유지.

<!-- KG: project_metahumotonic_web_integrate_core_dev_tech_2026_07_13, LakatosTree_MetahumotonicWebStack_20260713 -->
