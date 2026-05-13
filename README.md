# CodeReferee

CodeReferee is an SRE-aware AI coding pipeline. It receives a natural-language request, drafts Python code, reviews it from a reliability perspective, executes it in an isolated Docker sandbox, and loops through self-healing patches until the judge accepts the result or the retry limit is reached.

## MVP Scope

- FastAPI AI core for request intake and job execution
- Planner, Draft, Critic, Refiner, and Judge agent nodes
- Shared in-memory job state for local development
- Docker-based isolated Python sandbox
- Redis, PostgreSQL, and Prometheus services prepared through Docker Compose
- Korean-friendly API examples and architecture notes

The Spring Boot API gateway from the full architecture can be added as the next layer. This repository starts with the AI core because it is the system's highest-risk path.

## Quick Start

```bash
cd ai-core
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

In another terminal:

```bash
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{"requirement":"외부 API를 호출하되 2초 타임아웃과 최대 3회 재시도를 넣은 Python 코드를 작성해줘."}'
```

Check the result:

```bash
curl http://localhost:8000/jobs/{job_id}
```

## Agent-Critic Pipeline First

Use this endpoint when you want to test only the coding agent and critic loop before connecting Docker sandbox, Judge, Redis, or Spring Boot.

```bash
curl -X POST http://localhost:8000/pipelines/agent-critic \
  -H "Content-Type: application/json" \
  -d '{"requirement":"외부 API 호출 코드를 안정적으로 작성해줘.","refine":true}'
```

You can also pass existing code for review:

```bash
curl -X POST http://localhost:8000/pipelines/agent-critic \
  -H "Content-Type: application/json" \
  -d '{"requirement":"HTTP 호출 안정성 리뷰","current_code":"import urllib.request\nprint(urllib.request.urlopen(\"https://example.com\").read())"}'
```

## Local Infrastructure

```bash
docker compose up -d redis postgres prometheus
```

The sandbox runner uses Docker, so Docker Desktop must be running before executing jobs.

## Environment

`GOOGLE_API_KEY` enables Gemini-backed agents. If it is missing, the MVP falls back to deterministic local agent stubs so that the API and sandbox flow can still be tested.

## Repository Layout

```text
ai-core/
  app/
    agents/       # Planner, Draft, Critic, Refiner, Judge
    sandbox/      # Docker execution layer
    storage/      # Local job state
    workflow/     # Self-healing orchestration
    main.py       # FastAPI app
docs/
  architecture.md
infra/
  prometheus/
docker-compose.yml
```
