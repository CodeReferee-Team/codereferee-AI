# CodeReferee Sandbox

CodeReferee Sandbox는 GitHub 레포지토리를 격리된 환경에서 clone하고, build/test/run smoke validation을 수행하는 모듈이다.

## 역할

- GitHub repository clone
- branch 또는 commit checkout
- 프로젝트 stack 감지
- build/test/run 명령 실행
- timeout 및 resource limit 적용
- stdout/stderr/exit_code 수집
- Judge Agent에 전달할 실행 결과 생성

## 실행 흐름

```text
Repository URL
→ Preflight
→ Docker Sandbox
→ Clone Repository
→ Detect Stack
→ Run Build/Test/Smoke Check
→ Collect Logs
→ Return SandboxResult
```
