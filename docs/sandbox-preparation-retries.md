# Sandbox 준비 시간 제한·재시도 연결

외부 Sandbox 호출에 서비스 설정을 선택적으로 전달한다. 모델·Judge·자가치유 루프는 기존 구현을 사용한다.

| AI 환경 변수 | Sandbox 필드 | 기본값 |
| --- | --- | --- |
| `SANDBOX_PREPARATION_TIMEOUT_SECONDS` | `preparationTimeoutSeconds` | 미전달 |
| `SANDBOX_PREPARATION_MAX_RETRIES` | `maxPreparationRetries` | 0·미전달 |
| `SANDBOX_RETRY_DOWNLOADS` | `retryDownloads` | false·미전달 |
| `SANDBOX_PHASE_TIMEOUTS` | `phaseTimeouts` | 빈 객체·미전달 |

단계 객체는 JSON이며 `clone`, `install`, `build`, `test`, `image_build`, `image_load`, `rollout`의 1~7200초 정수를 받는다. 준비 재시도 횟수는 최대 1이다. 기존 `SANDBOX_HTTP_TIMEOUT_SECONDS`에서 30초를 뺀 값을 전체 상한으로 전달하는 동작을 유지한다.

아래 값은 실제 시간 연장 E2E에 사용한 짧은 설정이다.

```dotenv
SANDBOX_HTTP_TIMEOUT_SECONDS=510
SANDBOX_PREPARATION_TIMEOUT_SECONDS=300
SANDBOX_PREPARATION_MAX_RETRIES=1
SANDBOX_RETRY_DOWNLOADS=false
SANDBOX_PHASE_TIMEOUTS={"install":12,"image_build":90,"image_load":90,"rollout":60}
```

Sandbox가 설치 12초 시간 초과 시 정리를 확인하고 24초로 다시 실행한다. 전체 상한 480초는 유지된다. 운영 기본값은 팀 합의 후 설정한다. Backend 요청마다 사용자가 입력해야 하는 옵션은 추가하지 않았다.

같은 요청 ID·패치·설정으로 Sandbox를 다시 호출하면 완료 결과를 재사용한다. 새로운 측정을 의도하면 새 검증 요청 ID를 사용한다. 별도 준비 재시도 옵션을 기본 활성화하기 전에는 이 기능을 지원하는 Sandbox 버전의 배포가 필요하다.

## 팀 합의 필요

- 운영 기본 준비·전체 제한시간과 준비 재시도 1회 활성화 여부
- AI HTTP 대기와 Backend stale 시간 정합성
- 사용자에게 보여줄 재시도·결과 재사용 안내

## 실제 검증

Backend 제출부터 Redis·AI worker·Sandbox·결과 반환·PostgreSQL 저장까지 세 건을 실행했다.

- 다운로드가 두 번 끊긴 뒤 설치 단계에서 재시도해 PASSED
- 설치 12초 초과 후 자원을 정리하고 24초로 재시도해 PASSED
- 전체 시간이 부족한 경우 추가 재시도를 멈추고 ERROR

세 건 모두 실행 중 중복 요청 차단, 같은 Redis 입력 메시지를 재전달한 경우 저장 결과 재사용, 시도 이력 저장과 자원 회수를 확인했다. 작은 제어된 Python 레포와 다운로드 서버를 사용했다. 실측 원본은 로컬 `tmp/timeout-retry-20261111/e2e-final`에 보존한다.

Windows 계약·heartbeat 테스트 9개는 통과했다. Linux 전체 pytest는 436개 통과·3개 제외·수리 평가 2개 실패였다. 두 평가는 변경 전 HEAD의 설정·Sandbox 요청 코드에서도 동일하게 실패해 AI 팀 후속 확인 사항으로 남긴다. 모델·Judge 코드는 변경하지 않았다.

AWS·Mac 실행과 실제 LLM 자동 패치 생성은 추가 검증 범위다.
