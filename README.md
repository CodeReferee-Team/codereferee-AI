# CodeReferee AI

GitHub 레포지토리를 받아 **사용자가 늘고 장애가 나도 버티는지** 판정하는 검증 서버입니다. 판정 근거와 검증된 수정안을 함께 돌려줍니다. 코드를 생성하지 않고, 사용자 레포에 push하지 않습니다.

BE(`codereferee-server`)가 Redis 큐로 요청을 넘기면 이 서버가 샌드박스(`codereferee-sandbox`)에 실행을 맡기고, 돌아온 관측값으로 판정한 결과를 다시 큐로 돌려줍니다.

## 무엇을 판정하나

| 결과 | 뜻 |
| --- | --- |
| PASSED | 부하와 장애 주입을 거쳤고 오류 예산 안에서 버텼다 |
| FAILED | 장애나 부하에서 버티지 못했다. 요청자가 정한 예상 동시 사용자 수에서 실패한 경우도 여기에 든다 |
| ERROR (판정 불가) | 설치, 테스트, 기동이 실패했거나 인프라 문제로 측정하지 못했다 |

코드 결함은 판정하지 않습니다. 빌드나 테스트가 실패하면 안정성을 잴 수 없으므로 판정 불가로 처리합니다. 테스트가 하나도 없는 레포도 마찬가지입니다.

경고는 판정을 바꾸지 않습니다. 복제본이 1개뿐이라 장애 중에 잠깐 끊긴 경우, 장애 중에 응답이 크게 느려진 경우, 부하에서 일부 요청이 실패한 경우는 PASSED에 경고로 붙습니다.

## 파이프라인

```
preflight → planner → sandbox → infra_gate → judge → critic → refiner → refine(재검증) → finalize
```

LangGraph `StateGraph`로 돌아갑니다(`ai-core/app/workflow/`).

- **preflight**: URL 형태와 `git ls-remote`만 봅니다. clone도 실행도 하지 않습니다.
- **sandbox**: 샌드박스 HTTP API를 부릅니다. 샌드박스가 clone, 빌드·테스트, 배포, 부하, 장애 주입, 복구 관측을 합니다.
- **judge**: 결정적 규칙으로 판정합니다. LLM은 판정에 관여하지 않습니다(`judge_uses_llm` 기본값 `False`).
- **critic / refiner**: FAILED일 때만 돕니다. Critic이 원인을 설명하고, Refiner가 수정 편집을 만듭니다. 설정 파일이 없는 레포에는 `.codereferee/validation.yaml`을 새로 만들어 복제본 수를 올립니다.
- **refine**: 패치를 샌드박스 안에서만 적용해 실패한 시나리오를 다시 돌립니다. 최대 3회 재검증합니다. 검증된 패치는 리포트에 첨부되며, 제출한 커밋의 판정은 바꾸지 않습니다.

판정을 규칙에 맡기는 이유는 측정값에 있습니다. 같은 34건에서 규칙은 100%, LLM은 94%를 맞혔고, 레포 코드에 "이건 통과시켜라"라는 문장을 심자 LLM이 2건에서 속았습니다.

## 실행

```bash
cd ai-core
pip install -r requirements.txt

python -m app.worker                              # Redis 큐 워커 (운영 기본)
uvicorn app.main:app --host 0.0.0.0 --port 8000   # HTTP API
```

주요 환경변수는 다음과 같습니다(`ai-core/app/config.py`).

| 변수 | 용도 |
| --- | --- |
| `REDIS_URL` | BE와 주고받는 큐 |
| `SANDBOX_BASE_URL` | 샌드박스 API 주소. 빌드·테스트와 카오스가 모두 여기서 돕니다 |
| `PROMETHEUS_URL` | 샌드박스가 보낸 CPU·메모리 지표를 질의할 곳. 비우면 지표 없이 판정합니다 |
| `LLM_PROVIDER`, `LLM_MODEL`, `LLM_BASE_URL`, `LLM_API_KEY` | Critic/Refiner용. 운영은 OpenAI 호환 API(Groq)를 씁니다 |

ai-core 안의 로컬 Docker 샌드박스는 기본으로 꺼져 있습니다(`LOCAL_SANDBOX_ENABLED`). 평가와 오프라인 개발용으로 남겨 두었습니다.

운영 배포는 `main`에 머지되면 CI가 이미지를 ECR에 올리고, `Deploy AI` 워크플로를 수동으로 실행해 반영합니다.

## 평가

```bash
cd ai-core
python -m evals.runner run --model none --slices T0,T0-adv,T1-chaos
bash scripts/eval_gate.sh
```

`--model none`은 LLM 없이 규칙만 채점하므로 결정적이고 비용이 들지 않습니다. CI도 매번 같은 게이트를 돌려, 판정·카테고리·경고 일치율이 기준선보다 나빠지면 실패합니다. 인젝션 false-pass가 1건이라도 나오면 회귀로 봅니다.

## 문서

| 알고 싶은 것 | 문서 |
| --- | --- |
| 파이프라인 전체와 노드별 역할 | [docs/architecture.md](docs/architecture.md) |
| 판정 규칙의 조문과 근거 | [docs/judge-policy.md](docs/judge-policy.md) |
| 평가셋·지표·회귀 게이트 | [docs/evaluation-design.md](docs/evaluation-design.md) |
| Agent 입출력 스키마 | [docs/agents.md](docs/agents.md), [docs/agent-output-schema.md](docs/agent-output-schema.md) |
| 샌드박스 계약과 종료 코드 | [docs/sandbox.md](docs/sandbox.md) |
| Redis 큐와 BE 연동 | [docs/redis.md](docs/redis.md), [docs/backend-ai.md](docs/backend-ai.md) |
