# Architecture

## Runtime Flow

1. The client sends a public GitHub repository URL to `POST /jobs` for queued validation, or `POST /v1/validations/repository` for synchronous local smoke validation.
2. The AI core stores a queued validation job in local state and pushes a `repository_validation` task to Redis.
3. A worker dequeues the job and runs the validation workflow.
4. Preflight validates the URL shape and requested ref with `git ls-remote`.
5. Unreachable or unsupported links fail before expensive sandbox execution and are passed to Critic/Refiner for feedback.
6. Clone and smoke validation run inside Docker with CPU, memory, process, and timeout limits.
7. The sandbox emits logs and Prometheus-style metrics such as exit code, timeout, and duration.
8. Judge decides pass/fail from preflight, logs, and metrics.
9. Critic identifies the reliability gap and root cause.
10. Refiner returns remediation guidance, verification steps, and risk level; it does not generate replacement project code.

## Full System Mapping

- Spring Boot API server: external API gateway and future authentication/state persistence
- FastAPI AI core: repository-validation workflow and agent reports
- Docker sandbox: isolated clone/build/test smoke validation
- PostgreSQL: future durable report and execution history storage
- Redis: async repository-validation queue and task buffering
- Prometheus: metrics exposure and time-series validation evidence
- Judge agent: LLM-as-a-judge pass/fail decision
- Critic/Refiner agents: root cause and remediation report generation

## Removed Scope

Draft/code-generation has been removed. CodeReferee now validates existing repositories and reports how to improve them.
