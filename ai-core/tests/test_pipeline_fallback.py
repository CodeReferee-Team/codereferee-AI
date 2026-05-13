from app.models import AgentState, JobStatus, SandboxResult
from app.agents.nodes import judge_node, planner_node
from app.models import AgentCriticRequest
from app.workflow.agent_critic import run_agent_critic_pipeline


def test_planner_fallback_builds_plan() -> None:
    state = AgentState(job_id="test", requirement="write reliable python code")
    result = planner_node(state)
    assert result.plan["objective"] == "write reliable python code"
    assert "timeout" in result.plan["sre_considerations"]


def test_judge_fails_timeout() -> None:
    state = AgentState(
        job_id="test",
        requirement="anything",
        execution_result=SandboxResult(exit_code=None, timed_out=True),
    )
    result = judge_node(state)
    assert result.status == JobStatus.failed
    assert result.error_count == 1


def test_agent_critic_pipeline_reviews_existing_code() -> None:
    result = run_agent_critic_pipeline(
        AgentCriticRequest(
            requirement="HTTP 호출 안정성 리뷰",
            current_code='import urllib.request\nprint(urllib.request.urlopen("https://example.com").read())',
        )
    )
    assert result.initial_code != result.refined_code
    assert "timeout" in result.critic_feedback["issue"]
    assert "timeout" in result.refined_code
    assert "https://example.com" in result.refined_code
