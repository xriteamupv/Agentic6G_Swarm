"""A2A server for a robot agent (SPEC 8.1, 9.3)."""

from a6g_common.messages import NL
from a6g_common.schemas import Award, CallForProposals, Envelope
from a6g_transport.messaging import A6GMessage, MessageType, build_agent_app, build_agent_card
from starlette.applications import Starlette

from a6g_robot_agent.runtime import RobotAgent

AGENT_SKILLS = (
    ("envelope", "Evaluate envelope", "Accept, refuse or renegotiate a task envelope."),
    ("cfp", "Contract-net bid", "Bid on a peer's call for proposals."),
    ("award", "Contract-net award", "Accept an awarded task (idempotent by award_id)."),
    ("operator", "Operator command", "hold | resume."),
    ("state", "Robot state", "Current RobotState (pose, battery, link, armed)."),
)


def build_robot_card(agent: RobotAgent, base_url: str):
    m = agent.deps.manifest
    return build_agent_card(
        name=f"a6g-robot-{agent.robot_id}",
        description=(
            f"Agentic6G robot agent for {agent.robot_id} ({m.platform.value}); capabilities: "
            + ", ".join(c.value for c in m.capabilities)
        ),
        base_url=base_url,
        skills=AGENT_SKILLS,
    )


def build_robot_app(agent: RobotAgent, base_url: str, *, handlers: list | None = None) -> Starlette:
    async def handle(msg: A6GMessage) -> A6GMessage:
        if msg.type is MessageType.ENVELOPE:
            reply = await agent.handle_envelope(Envelope.model_validate(msg.payload))
            return A6GMessage(
                MessageType.ENVELOPE_REPLY,
                reply.model_dump(mode="json"),
                NL(rationale=reply.justification) if reply.justification else None,
            )
        if msg.type is MessageType.CFP:
            nl = msg.nl.model_dump(exclude_none=True) if msg.nl else None
            bid = await agent.handle_cfp(CallForProposals.model_validate(msg.payload), nl=nl)
            return A6GMessage(MessageType.BID, bid.model_dump(mode="json"))
        if msg.type is MessageType.AWARD:
            ack = await agent.handle_award(Award.model_validate(msg.payload))
            return A6GMessage(MessageType.AWARD_ACK, ack)
        if msg.type is MessageType.STATE:
            state = await agent.get_robot_state()
            return A6GMessage(MessageType.STATE_REPLY, state.model_dump(mode="json"))
        if msg.type is MessageType.QUEUE:
            return A6GMessage(MessageType.QUEUE_REPLY, await agent.task_queue())
        if msg.type is MessageType.OPERATOR:
            await agent.handle_operator(
                str(msg.payload["command"]), msg.payload.get("reason", "operator")
            )
            return A6GMessage(MessageType.OPERATOR_ACK, {"ok": True})
        return A6GMessage(MessageType.ERROR, {"error": f"unsupported message {msg.type.value}"})

    return build_agent_app(
        build_robot_card(agent, base_url), handle, agent_id=agent.robot_id, handlers=handlers
    )
