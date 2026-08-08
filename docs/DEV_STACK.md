# 개발 스택 — Git 조율 · 측정 · 판정

> 이 repo(그리고 자매 [`metahumotonic-web`](https://github.com/gj3447/metahumotonic-web))는
> 사용자 자작 3층 개발기술 위에서 개발한다. 상위 정본:
> `SYMPOSIUM/GIT/delltower_import/CLAUDE.md`.

| 층 | 도구 | 이 repo에서의 역할 |
|---|---|---|
| **조율** | **canonical `main` 단일 writer** | 기존 변경을 보존하고 exact path만 stage/commit한다. OMD는 퇴역했다. |
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

## 조율 — OMD 퇴역, Git 단일 writer

OMD queue, lease, heartbeat, worktree driver는 사용하지 않는다. 과거 기록과 퇴역 사유는
[`OMD_PARALLEL.md`](OMD_PARALLEL.md)에 남아 있으며 활성 프로토콜이 아니다.

현재 규율:

1. 작업 전 `git status --short --branch`와 worktree 목록을 확인한다.
2. 다른 세션의 tracked/untracked 변경은 수정·이동·삭제하지 않는다. 겹치면 중단한다.
3. canonical checkout의 tracking `main` 한 곳에서만 쓴다. 임시 병렬 worktree를 자동 생성하지 않는다.
4. 검증 후 소유한 exact path만 stage/commit한다. `git add -A`와 디렉터리 통째 stage는 금지한다.
5. push 뒤 local `main`과 `origin/main` exact readback을 확인한다.

운영 진단은 개발 조율과 분리한다. DGX의 무설정 `kubectl`이나 퇴역 manifest 대신
[`OPERATIONS_VM100.md`](OPERATIONS_VM100.md)와 `ops/check-web-back-live.sh`를 사용한다.

<!-- KG: project_metahumotonic_web_integrate_core_dev_tech_2026_07_13, LakatosTree_MetahumotonicWebStack_20260713 -->
