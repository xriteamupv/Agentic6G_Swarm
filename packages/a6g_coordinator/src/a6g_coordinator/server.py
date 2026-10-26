"""Coordinator graph entry point for the LangGraph Agent Server (``langgraph.json``).

``langgraph dev`` (or a LangGraph Platform / self-hosted deployment) serves this graph to
agent-chat-ui, the ``a6g`` CLI and the A2A gateway. The server supplies the checkpointer
(Postgres in production) and the store, so the coordinator process is stateless.

Environment: ``A6G_CONFIG_DIR`` (default ``config``), ``A6G_FLEET_FILE`` (e.g.
``sim/fleet.yaml``), ``A6G_COORDINATOR_LLM=off`` to use templates only, and the model
provider key (``OPENAI_API_KEY``, read from ``.env``).
"""

import dataclasses
import logging
import os

from a6g_common.clock import SystemClock
from a6g_common.config import ModelRole, load_config
from a6g_common.ids import new_id
from a6g_common.models import chat_model
from a6g_interactions import InteractionClient
from a6g_kb import KnowledgeClient
from a6g_observability.decision_log import DecisionLog

from a6g_coordinator.graph import CoordinatorDeps, build_graph
from a6g_coordinator.links import A2ALinkFactory

log = logging.getLogger(__name__)


def _model(cfg):
    if os.environ.get("A6G_COORDINATOR_LLM", "on").lower() in ("off", "0", "false", "no"):
        return None
    try:
        return chat_model(ModelRole.COORDINATOR_LLM, cfg.models)
    except Exception as exc:  # e.g. no OPENAI_API_KEY: deterministic templates still work
        log.warning("coordinator LLM unavailable (%s); using templates", exc)
        return None


def make_deps() -> CoordinatorDeps:
    cfg = load_config(os.environ.get("A6G_CONFIG_DIR", "config"))
    return CoordinatorDeps(
        cfg=cfg,
        links=A2ALinkFactory(cfg.fleet.defaults.coordinator.envelope_timeout_s),
        decisions=DecisionLog(cfg.fleet.edge.decision_log_path),
        clock=SystemClock(),
        ids=new_id,
        model=_model(cfg),
        kb=KnowledgeClient(
            cfg.knowledge.url, agent_id="coordinator", timeout_s=cfg.knowledge.timeout_s
        ),
        interactions=InteractionClient(
            cfg.interactions.url, agent_id="coordinator", timeout_s=cfg.interactions.timeout_s
        ),
    )


def model_first(deps: CoordinatorDeps, assistant_id: str = "coordinator") -> CoordinatorDeps:
    """The same coordinator with ``understanding: model_first`` (2026-10-22): the model
    plans every order and checks the assignment after publication; the rules only as the
    fallback."""
    c = deps.cfg
    coord = c.fleet.defaults.coordinator.model_copy(
        update={"understanding": "model_first", "assistant_id": assistant_id}
    )
    defaults = c.fleet.defaults.model_copy(update={"coordinator": coord})
    cfg = c.model_copy(update={"fleet": c.fleet.model_copy(update={"defaults": defaults})})
    return dataclasses.replace(deps, cfg=cfg)


#: The graph's dependencies (the gateway records admitted robots in ``DEPS.admitted``).
#: ``A6G_UNDERSTANDING=model_first`` at launch selects the model-first coordinator; the
#: default (and anything else) is the rules-first one. One graph is built, under one name.
DEPS = make_deps()
if os.environ.get("A6G_UNDERSTANDING", "").strip().lower() == "model_first":
    DEPS = model_first(DEPS)
    log.warning("coordinator: model-first understanding (A6G_UNDERSTANDING=model_first)")
graph = build_graph(DEPS).compile()
graph.name = "Agentic6G Swarm Coordinator"
