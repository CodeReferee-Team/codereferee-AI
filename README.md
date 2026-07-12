# CodeReferee Redis Worker

CodeReferee의 Redis 모듈은 백엔드와 AI 서버 사이에서 검증 작업을 비동기로 처리하기 위한 큐 역할을 한다.
Redis는 결과를 영구 저장하는 DB가 아니라, 처리해야 할 검증 작업을 잠시 보관하는 작업 대기열로 사용된다.

---

## 1. Redis 구조 개요

현재 Redis 기반 처리 흐름은 다음과 같다.

```text
Backend
→ Redis Queue
→ AI Worker
→ Repository Validation Workflow
→ Sandbox / Judge / Critic / Refiner
→ Result
```

## 2. 주요 역할
**Redis Queue**
Redis Queue는 검증 작업을 저장하고 Worker에게 전달한다.
- 검증 작업 enqueue
- 검증 작업 dequeue
- job_id 기반 작업 전달
- 비동기 처리 지원

**AI Worker**
AI Worker는 Redis Queue에서 작업을 가져와 검증 workflow를 실행한다.
- Redis Queue 감시
- 작업 1개 또는 반복 처리
- Repository Validation Workflow 실행
- 처리 결과 상태 업데이트

## 3. 실행 방식
단일 작업 처리
python -m app.worker --once
실시간 작업 처리
python -m app.worker

## 4. 관련 파일
**ai-core/app/queue/redis_queue.py**
Redis enqueue/dequeue 로직을 담당한다.
**ai-core/app/worker.py**
Redis Queue에서 작업을 꺼내 검증 workflow를 실행한다.
**ai-core/app/config.py**
Redis URL, queue name 등 실행 설정을 관리한다.
**ai-core/app/models.py**
Job 상태와 요청/응답 데이터 모델을 정의한다.
**ai-core/app/workflow/repository_validation.py**
Worker가 가져온 작업을 실제 검증 workflow로 연결한다.

