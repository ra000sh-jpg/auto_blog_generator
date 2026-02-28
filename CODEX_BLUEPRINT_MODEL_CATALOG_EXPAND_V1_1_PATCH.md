# V1.1 PATCH — Model Catalog Expand 보정

> 코덱스 리뷰 P0~P2 4건을 반영하는 보정 패치.
> 원본 `CODEX_BLUEPRINT_MODEL_CATALOG_EXPAND.md`의 PATCH 1(TEXT_MODEL_MATRIX 확장)은 그대로 유지하고,
> 아래 패치를 **추가로** 적용한다.

---

## PATCH-FIX 1 — registered_models 자동 동기화 (P0 수정)

### 문제
`TEXT_MODEL_MATRIX`를 확장해도 `router_registered_models`에 반영되지 않으면
eval/champion 시스템(`cycle_run_daily_model_eval`, `cycle_auto_champion_switch`)에 신규 모델이 진입하지 않는다.
현재 `save_settings()`에 registered_models 저장 로직이 없다.

### 수정

**파일: `modules/llm/llm_router.py`**

`save_settings()` 함수 맨 끝, `return normalized` 직전(현재 line 616)에 registered_models 자동 동기화 블록을 추가한다.

현재 코드:
```python
            if challenger_model_raw:
                self.job_store.set_system_setting("router_challenger_model", challenger_model_raw)

        return normalized
```

변경 후:
```python
            if challenger_model_raw:
                self.job_store.set_system_setting("router_challenger_model", challenger_model_raw)

            # ── registered_models 자동 동기화 ──
            # TEXT_MODEL_MATRIX에서 키가 설정된 모델을 registered_models에 병합한다.
            # 기존에 운영자가 active=false로 설정한 모델은 덮어쓰지 않는다.
            self._sync_registered_models(text_keys)

        return normalized
```

동일 클래스 내에 `_sync_registered_models` 메서드를 추가한다.
`save_settings()` 바로 아래(현재 `build_plan()` 직전, line 618 부근)에 삽입:

```python
    def _sync_registered_models(self, text_api_keys: Dict[str, str]) -> None:
        """TEXT_MODEL_MATRIX + 설정된 키 기반으로 registered_models를 병합 동기화한다.

        규칙:
        - 키가 있는 provider의 모델만 대상
        - 이미 등록된 모델의 active 상태는 유지 (운영자 제어 존중)
        - 신규 모델은 active=true로 추가
        - 키가 없어진 provider의 모델은 제거하지 않음 (이력 보존)
        """
        if not self.job_store:
            return

        import json

        raw = self.job_store.get_system_setting("router_registered_models", "[]")
        try:
            existing: list = json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            existing = []

        # 기존 등록 맵: model_id -> entry
        existing_map: Dict[str, dict] = {}
        for entry in existing:
            if isinstance(entry, dict):
                mid = str(entry.get("model_id", "")).strip()
                if mid:
                    existing_map[mid] = entry

        # TEXT_MODEL_MATRIX에서 키가 설정된 모델
        available_specs = [
            spec for spec in TEXT_MODEL_MATRIX
            if str(text_api_keys.get(spec.key_id, "")).strip()
        ]

        changed = False
        for spec in available_specs:
            if spec.model not in existing_map:
                existing_map[spec.model] = {
                    "model_id": spec.model,
                    "provider": spec.provider,
                    "active": True,
                }
                changed = True

        if changed:
            updated = list(existing_map.values())
            self.job_store.set_system_setting(
                "router_registered_models",
                json.dumps(updated, ensure_ascii=False),
            )
```

### 동작 설명
1. Settings 저장 시 `_sync_registered_models()` 호출
2. `TEXT_MODEL_MATRIX`에 있고 API 키가 설정된 모델 중 `router_registered_models`에 없는 것을 `active=true`로 추가
3. 운영자가 기존에 `active=false`로 설정한 모델은 건드리지 않음
4. 이로써 **매트릭스 확장 + Settings 저장 1회 → 신규 모델이 eval/champion 후보로 자동 진입**

---

## PATCH-FIX 2 — 견적 설명 문구 정정 (P1 수정)

### 문제
원본 블루프린트 "동작 확인" 섹션 2번:
> "키가 있는 모든 모델의 비용을 합산"

실제 코드: `build_plan()` → `_assign_roles()` → 역할별(parser/quality/voice) 선택 모델 기반 견적 계산.

### 수정

원본 블루프린트의 해당 섹션을 아래로 **교체**한다 (코드 변경 아님, 문서 보정):

```
### 2. 실시간 견적 (quote)
- `build_plan()`이 `_available_text_specs()`로 키가 있는 모델 풀을 구성
- `_assign_roles()`가 전략 모드에 따라 풀에서 역할별(parser/quality/voice 등) 모델을 선택
- 견적은 **선택된 역할별 모델의 비용을 합산** (풀 전체 합산이 아님)
- 같은 provider의 다수 모델이 풀에 있으면, 전략에 따라 저가 모델은 parser/보조 역할에, 고가 모델은 quality 역할에 배정
- 매트릭스 확장으로 역할별 최적 배정 후보가 늘어남 → 견적 정확도 개선
```

---

## PATCH-FIX 3 — champion 우선 고정 명시 (P1 수정)

### 문제
원본 블루프린트가 "키 하나로 자동 활성화"를 설명하면서, champion_model이 quality_step을 고정하는 동작을 언급하지 않음.

### 수정

원본 블루프린트 "설계 원칙" 섹션에 항목 추가 (코드 변경 아님, 문서 보정):

```
5. `champion_model`이 설정되어 있으면 quality_step은 해당 모델로 고정된다.
   신규 모델이 quality_step에 배정되려면 eval → champion 교체 과정을 거쳐야 한다.
   매트릭스 확장은 주로 parser/pre_analysis/sentence_polish 등 **보조 역할의 후보 풀**을 넓히고,
   eval 경쟁을 통해 champion 교체 기회를 제공하는 것이 핵심 가치이다.
```

---

## PATCH-FIX 4 — 변경 파일 목록 정정 (P2 수정)

### 문제
원본 상단에 `provider_factory.py`를 변경 대상으로 표기했지만, 본문에서는 "변경 없음"으로 기술.

### 수정

원본 블루프린트 상단 헤더를 아래로 **교체** (문서 보정):

```
> **변경 파일**: 1개
> - `modules/llm/llm_router.py` (TEXT_MODEL_MATRIX 확장 + 가격 보정 + registered_models 동기화)
>
> **변경하지 않는 파일**:
> - `modules/llm/provider_factory.py` — 모든 신규 모델이 기존 factory로 생성 가능 (검증 완료)
> - `scheduler_cycles.py` — eval/champion 로직 변경 없음
> - `job_store.py` — 스키마 변경 없음
> - `server/routers/router_settings.py` — 응답 구조 변경 없음
> - 프론트엔드 — 기존 모델 매트릭스 테이블이 자동으로 확장 표시
```

---

## 최종 변경 파일 요약 (V1 + V1.1 통합)

| 파일 | 변경 내용 |
|------|-----------|
| `modules/llm/llm_router.py` | PATCH 1: TEXT_MODEL_MATRIX 7→16개 + qwen-plus 가격 보정 |
| `modules/llm/llm_router.py` | PATCH-FIX 1: `_sync_registered_models()` 메서드 추가 + `save_settings()` 끝에 호출 |

## 추가 검증 체크리스트 (V1 체크리스트에 추가)

```bash
# 6. registered_models 동기화 확인
python3 -c "
from modules.llm.llm_router import LLMRouter
# _sync_registered_models 메서드 존재 확인
assert hasattr(LLMRouter, '_sync_registered_models'), 'Method not found'
print('_sync_registered_models exists')
"

# 7. 기존 테스트 통과 (재확인)
python3 -m pytest tests/ -x -q --ignore=tests/e2e
```

## 수용 기준 (V1.1 추가분)

1. `save_settings()` 호출 시 키가 있는 신규 모델이 `router_registered_models`에 자동 추가된다
2. 기존에 `active=false`로 설정된 모델의 상태가 유지된다
3. 블루프린트 문서에서 "비용 합산" 표현이 "역할별 선택 모델 비용 합산"으로 정정되어 있다
4. 변경 파일이 `llm_router.py` 1개로 정리되어 있다
