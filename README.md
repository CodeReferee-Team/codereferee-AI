# CodeReferee

CodeReferee is an SRE-aware repository validation platform. It accepts an existing public GitHub repository URL, stores the link/commit metadata, clones the repository inside a sandbox, runs smoke validation under resource limits, and uses Judge/Critic/Refiner agents to produce a reliability report.

## MVP Scope

- FastAPI AI core for repository validation intake
- GitHub URL preflight before expensive sandbox execution
- Docker-based repository clone and smoke-test sandbox
- Judge, Critic, and Refiner-report agent nodes
- Prometheus metrics endpoint for validation counters/durations
- Spring Boot API gateway integration through `/v1/validations/repository`

Code generation has been removed from the MVP. CodeReferee validates existing projects instead of drafting new code.

## Quick Start

```bash
cd ai-core
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

Run local infrastructure when using queued jobs:

```bash
docker compose up -d redis
```

Run a fixed local sandbox pool for repository execution:

```bash
docker compose up -d --build sandbox-gateway sandbox-1 sandbox-2 sandbox-3
```

When AI Core runs on the same host, configure:

```env
SANDBOX_BASE_URL=http://localhost:8100
SANDBOX_REPOSITORY_PATH=/repositories/validate
SANDBOX_HTTP_TIMEOUT_SECONDS=240
```

Validate a repository synchronously:

```bash
curl -X POST http://localhost:8000/v1/validations/repository \
  -H "Content-Type: application/json" \
  -d '{"repository_url":"https://github.com/CodeReferee-Team/codereferee-AI","branch":"main"}'
```

Enqueue a repository validation job through Redis:

```bash
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{"repository_url":"https://github.com/CodeReferee-Team/codereferee-AI","branch":"main"}'
```

Run a worker once:

```bash
python -m app.worker --once
```

Check a saved job:

```bash
curl http://localhost:8000/jobs/{job_id}
```

## Runtime Flow

```text
GitHub URL
  -> queued job state
  -> Redis queue
  -> worker dequeues job
  -> preflight URL/ref check
  -> validation plan
  -> Docker sandbox clone
  -> build/test/run smoke detection
  -> metrics/log collection
  -> Judge pass/fail
  -> Critic root-cause report
  -> Refiner remediation guidance
```

## Environment

`GOOGLE_API_KEY` enables Gemini-backed Judge/Critic/Refiner reports. If it is missing, the MVP uses deterministic local fallback reports so that API and sandbox flow can still be tested.

Docker Desktop must be running for sandbox execution.

## Repository Layout

```text
ai-core/
  app/
    agents/       # Planner, Judge, Critic, Refiner-report nodes
    repository/   # GitHub intake/preflight checks
    sandbox/      # Docker repository execution layer
    storage/      # Local job state
    workflow/     # Repository validation orchestration
    main.py       # FastAPI app
docs/
  architecture.md
infra/
  prometheus/
docker-compose.yml
```
