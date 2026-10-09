# Sandbox pod 진단 수집 스펙 (배포 실패 시)

상태: 제안 (2026-10-09). 대상 레인: Sandbox(태우·도훈). 소비자: AI-core Critic/Refiner.

## 왜

배포 롤아웃이 실패하면 지금 sandbox는 `kubectl rollout status` 타임아웃 문자열만 남긴다.
그래서 서로 다른 실패가 전부 같은 신호로 들어온다 — 실측(2026-10-09)에서 확인:

| fixture | 실제 원인 | sandbox가 준 신호 | Critic 결과 |
|---|---|---|---|
| http500-bug | `/`(readiness 경로)가 500 → 영구 not-ready | `timed out waiting for the condition` | "기동 느림"으로 **오진** |
| crashloop | 미선언 env → 부팅 크래시 → CrashLoopBackOff | `timed out waiting for the condition` | 동일 신호로 구분 불가 |

근본원인이 입력에 없어 Critic이 분류할 수 없다. 코드·env·probe 자가치유가 막힌 지점이 여기다.

## 무엇을 (수집 항목)

`wait_rollout`이 실패(비정상 종료)하면, 네임스페이스 정리 **전에** 해당 deployment pod들의
진단을 모은다. 최소 세트만으로도 세 실패 모드가 갈린다.

- `kubectl get pods -o json` → `status.containerStatuses[]`:
  - `state.waiting.reason` (예: `CrashLoopBackOff`, `ErrImagePull`, `ImagePullBackOff`, `CreateContainerError`)
  - `lastState.terminated.{reason, exitCode}` (크래시 종료 코드)
  - `ready`, `restartCount`
- `kubectl logs <pod> --previous --tail=50` (크래시 직전 로그 = 트레이스백). `--previous` 없으면 현재 로그 tail.
- pod events: `kubectl get events --field-selector involvedObject.name=<pod> -o json`
  의 `reason`/`message` (예: `Unhealthy` = readiness probe 실패, `BackOff`, `Failed`, `FailedScheduling`)

세 모드 판별:
- **부팅 크래시**: waiting.reason=CrashLoopBackOff + lastState.terminated.exitCode≠0 + logs에 트레이스백
- **readiness probe 실패**: pod Running인데 ready=false + event reason=Unhealthy(probe HTTP code)
- **이미지 풀 실패**: waiting.reason=ErrImagePull/ImagePullBackOff

## 어디에 (출력 계약)

`sandbox_report`에 새 키 `pod_diagnostics` 추가(배포 실패 시에만). schema 예:

```json
"pod_diagnostics": {
  "pods": [
    {
      "name": "app-7c9f-abc",
      "phase": "Running",
      "containers": [
        {"name": "app", "ready": false, "restart_count": 5,
         "waiting_reason": "CrashLoopBackOff",
         "last_terminated": {"reason": "Error", "exit_code": 1}}
      ],
      "logs_tail": "...KeyError: 'DATABASE_URL'...",
      "events": [{"reason": "BackOff", "message": "Back-off restarting failed container", "count": 5}]
    }
  ],
  "collected_at": "2026-10-09T20:25:00Z"
}
```

규칙: logs_tail 50줄 상한, 기존 시크릿 마스킹 재사용, 요청 네임스페이스 한정.

## AI-core 소비 (내 lane — 이 신호 오면 바로)

- evidence 패킷에 `pod_waiting_reason`·`last_terminated`·`logs_tail`·`probe_events` 추가
- `classify_failure_category`: 현재 `deploy_rollout_timeout`(단계만)을 세분
  → `deploy_crashloop` / `deploy_probe_failure` / `deploy_image_pull_error`
- Critic/Refiner가 근본원인별 수정 제안 가능 → **코드·env·probe 자가치유 개방**
  (crashloop: env 가드 코드 패치 / probe: healthPath·probe 설정 / imagepull: 태그·레지스트리)

현재(신호 오기 전) AI-core 동작: `deploy_rollout_timeout`으로 묶고 Critic은 "롤아웃 미준비,
근본원인 미확정(pod 로그 없음)"이라 **정직하게** 답한다(오진 안 함). → 이미 반영됨
(`evidence.py` `_is_rollout_timeout`, `prompts.py` CRITIC_PROMPT).

## 범위 밖

- liveness/startup probe 튜닝 자동화, OOM 메모리 계측(별도 — chaos evidence에 mem 없음)
- 전체 pod describe 덤프(노이즈 큼) — 위 최소 세트로 충분
