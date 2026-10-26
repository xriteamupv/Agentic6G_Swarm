"""Assemble a :class:`RobotAgent` from configuration."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from a6g_common.clock import Clock, SystemClock
from a6g_common.config import A6GConfig, LinkLossPolicy
from a6g_common.ids import IdFactory, new_id
from a6g_observability.decision_log import DecisionSink
from a6g_transport.budget import UplinkBudget
from a6g_transport.messaging import TelemetryBus
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver

from a6g_robot_agent.graph import AgentDeps
from a6g_robot_agent.peers import PeerDirectory
from a6g_robot_agent.perception import EdgeConfirm, PerceptionPipeline, StubTriage, Triage
from a6g_robot_agent.runtime import RobotAgent
from a6g_robot_agent.skills import SkillClient
from a6g_robot_agent.uplink import Uplink


def build_robot_agent(
    cfg: A6GConfig,
    robot_id: str,
    *,
    skills: SkillClient,
    checkpointer: BaseCheckpointSaver,
    uplink: Uplink,
    decisions: DecisionSink,
    triage: Triage | None = None,
    edge: EdgeConfirm | None = None,
    model: BaseChatModel | None = None,
    bus: TelemetryBus | None = None,
    clock: Clock | None = None,
    ids: IdFactory = new_id,
    poll_interval_s: float | None = None,
    anomaly_source: Callable[[str | None], str | None] | None = None,
    peers: PeerDirectory | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    gate: Callable[[], Any] | None = None,
    a2a_url: str | None = None,
    kb: Any = None,
    interactions: Any = None,
) -> RobotAgent:
    """``kb``: a KnowledgeClient (places, people routes); ``interactions``: an
    InteractionClient (SPEC A3.5). Without them the robot uses names and its own copy."""
    manifest = cfg.manifests[robot_id]
    if bus is not None and hasattr(bus, "as_agent"):
        bus = bus.as_agent(robot_id)  # its publications carry its own agent id
    defaults = cfg.fleet.defaults
    budget = UplinkBudget.from_config(robot_id, cfg.fleet.budget_for(robot_id))
    triage = triage or StubTriage(anomaly_source or (lambda _poi: None))
    perception = PerceptionPipeline(
        triage,
        edge,
        threshold=defaults.agent.triage_confidence_threshold,
        budget=budget,
        ids=ids,
    )
    deps = AgentDeps(
        robot_id=robot_id,
        manifest=manifest,
        campus=cfg.campus,
        allocator=cfg.allocator,
        agent_cfg=defaults.agent,
        link_policy=LinkLossPolicy(defaults.link_loss_policy),
        uplink=uplink,
        budget=budget,
        decisions=decisions,
        perception=perception,
        clock=clock or SystemClock(),
        ids=ids,
        model=model,
        knowledge=cfg.knowledge,
        kb=kb,
        interactions=interactions,
        icfg=cfg.interactions,
    )
    return RobotAgent(
        deps,
        skills,
        checkpointer,
        bus=bus,
        peers=peers,
        poll_interval_s=poll_interval_s,
        sleep=sleep or asyncio.sleep,
        gate=gate,
        coordinator_timeout_s=defaults.coordinator_timeout_s,
        a2a_failures_for_loss=defaults.a2a_failures_for_loss,
        cfp_deadline_ms=defaults.cfp_deadline_ms,
        a2a_url=a2a_url,
    )
