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

Validate a repository:

```bash
curl -X POST http://localhost:8000/v1/validations/repository \
  -H "Content-Type: application/json" \
  -d '{"repository_url":"https://github.com/CodeReferee-Team/codereferee-AI","branch":"main"}'
```

Check a saved job:

```bash
curl http://localhost:8000/jobs/{job_id}
```

## Runtime Flow

```text
GitHub URL
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
