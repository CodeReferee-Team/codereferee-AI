from uuid import uuid4

from app.agents.nodes import critic_node, draft_node, planner_node, refiner_node
from app.models import AgentCriticRequest, AgentCriticResponse, AgentState


def run_agent_critic_pipeline(request: AgentCriticRequest) -> AgentCriticResponse:
    state = AgentState(job_id=str(uuid4()), requirement=request.requirement)
    state = planner_node(state)

    if request.current_code:
        state.current_code = request.current_code
        state.events.append("Input: existing code loaded")
    else:
        state = draft_node(state)

    initial_code = state.current_code
    state = critic_node(state)

    if request.refine:
        state = refiner_node(state)
    else:
        state.events.append("Refiner: skipped by request")

    return AgentCriticResponse(
        requirement=state.requirement,
        plan=state.plan,
        initial_code=initial_code,
        critic_feedback=state.critic_feedback,
        refined_code=state.current_code,
        events=state.events,
    )
