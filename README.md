# CodeReferee Backend-AI Integration

CodeReferee의 Backend-AI 연동 모듈은 Spring Boot 백엔드와 FastAPI AI 서버가 검증 요청과 결과를 주고받기 위한 API 계약을 담당한다.

이 브랜치는 Agent 판단 로직이나 Sandbox 구현 자체보다, 백엔드가 AI 서버를 어떻게 호출하고 어떤 응답을 받는지 정리하는 것을 목표로 한다.

---

## 1. 연동 구조 개요

현재 Backend-AI 연동 흐름은 다음과 같다.

```text
Client
→ Spring Boot Backend
→ FastAPI AI Server
→ Repository Validation Workflow
→ Validation Response
→ Spring Boot Backend
```

## 2. 주요 API
**Health Check**
GET /health
AI 서버가 정상 실행 중인지 확인

**Repository Validation**
POST /v1/validations/repository
GitHub 레포지토리 검증을 요청

**Job 조회**
GET /jobs/{job_id}
검증 작업의 현재 상태와 결과 조회

## 3. 관련 파일
**ai-core/app/main.py**
FastAPI endpoint를 정의한다.
**ai-core/app/models.py**
백엔드와 AI 서버가 주고받는 요청/응답 모델을 정의한다.
**ai-core/app/config.py**
AI 서버 실행 설정과 외부 연동 설정을 관리한다.
**ai-core/app/workflow/repository_validation.py**
API 요청을 실제 레포지토리 검증 workflow로 연결한다.
