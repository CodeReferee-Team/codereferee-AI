# Architecture

## Runtime Flow

1. The client sends a natural-language coding requirement to `POST /jobs`.
2. The AI core creates a job state and runs the pipeline.
3. Planner converts the requirement into an SRE-aware implementation plan.
4. Draft creates initial Python code.
5. Critic reviews the code and recent execution logs.
6. Refiner applies a reliability patch.
7. Sandbox executes the code inside Docker with CPU, memory, process, and timeout limits.
8. Judge decides pass or fail from sandbox logs and metrics.
9. On failure, the loop returns to Critic until `max_retries` is reached.

## Full System Mapping

- Spring Boot API server: external API gateway, authentication, state manager
- Redis: async job queue and task buffering
- FastAPI AI core: multi-agent coding pipeline
- LangGraph: agent state graph orchestration
- Docker sandbox: isolated execution target
- PostgreSQL: reports and execution history
- Prometheus: metrics from API core and sandbox runs
- Judge agent: LLM-as-a-judge pass/fail decision

The current repository implements the FastAPI AI core and sandbox path first. Redis/PostgreSQL/Prometheus are wired for local infrastructure and ready for the next integration step.
