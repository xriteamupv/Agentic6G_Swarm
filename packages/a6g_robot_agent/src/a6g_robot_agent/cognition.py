"""Swarm Robotics Agent cognitive core: the model proposes, code disposes.

The robot's model (role ``onboard_vlm``) proposes typed actions for five functions:
envelope deliberation, local replanning, contract-net participation, conversation with a
person and report summaries (triage lives in :mod:`a6g_robot_agent.perception`). Code
validates every proposal against the envelope, the cost function's feasibility verdicts,
safety and the schema. A rejected proposal goes back to the model once with the
reason; if the retry is rejected too, or the model fails or times out, the deterministic
logic decides and the decision records ``fallback_rejected`` / ``fallback_no_model``.

Peers' and people's text reaches a prompt only inside a delimited DATA block;
instruction-like content in it is flagged and logged, and never changes a validator verdict.
"""

import asyncio
import contextlib
import inspect
import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, TypeVar

from a6g_common.messages import suspicious as _suspicious
from a6g_common.messages import untrusted as _untrusted
from a6g_common.models import MODEL_CALLER, structured_call
from a6g_common.schemas import RefusalReason
from a6g_interactions.language import stated_constraint  # noqa: F401 (re-exported)
from a6g_observability.comms import comms_log
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langsmith import tracing_context
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)
ROLE = "onboard_vlm"

# --------------------------------------------------------------------------- proposals


class Deliberation(BaseModel):
    """Envelope deliberation."""

    action: Literal["accept_all", "accept_subset", "refuse"]
    task_ids: list[str] = Field(default_factory=list, description="accept_subset: tasks taken")
    reason: RefusalReason | None = Field(None, description="refuse / tasks left out: why")
    order: list[str] | None = Field(None, description="order of the accepted tasks")
    rationale: str = Field("", max_length=400)


SkipReason = Literal["battery", "capability", "region", "conflict", "link", "unsafe", "blocked"]


class Replan(BaseModel):
    """Local replanning."""

    action: Literal["continue", "resequence", "skip", "escalate", "request_help", "hold"]
    order: list[str] | None = None
    task_id: str | None = None
    reason: SkipReason | None = None
    obs_id: str | None = None
    hold_s: float | None = None
    rationale: str = Field("", max_length=400)


class Participation(BaseModel):
    """Contract-net participation: bid values always come from the cost function."""

    participate: bool = True
    reason: RefusalReason | None = None
    note: str = Field("", max_length=300)


class Summary(BaseModel):
    summary: str = Field("", max_length=600)


# --------------------------------------------------------------------------- prompts


@dataclass(frozen=True)
class Prompt:
    role: str
    function: str
    version: str
    system: str

    @property
    def ref(self) -> str:
        return f"{self.role}/{self.function}@v{self.version}"


def _prompts_dir() -> Path:
    env = os.environ.get("A6G_PROMPTS_DIR")
    if env:
        return Path(env)
    for base in (Path.cwd(), *Path(__file__).resolve().parents):
        if (base / "prompts" / ROLE).is_dir():
            return base / "prompts"
    raise FileNotFoundError("prompts/ not found (set A6G_PROMPTS_DIR)")


@lru_cache(maxsize=32)
def load_prompt(function: str, role: str = ROLE) -> Prompt:
    """``prompts/<role>/<function>.md``: front matter (version) + sections."""
    text = (_prompts_dir() / role / f"{function}.md").read_text()
    version = "0"
    if text.startswith("---"):
        front, text = text[3:].split("---", 1)
        for line in front.splitlines():
            if line.strip().startswith("version:"):
                version = line.split(":", 1)[1].strip()
    return Prompt(role, function, version, text.strip())


# Rule 24 helpers, shared with the coordinator.
suspicious = _suspicious
untrusted = _untrusted


# --------------------------------------------------------------------------- propose


@dataclass
class Outcome:
    """A proposal that passed validation (``value``), or why the code decided instead."""

    value: Any = None
    policy: str = "fallback_no_model"  # model | fallback_no_model | fallback_rejected
    attempts: int = 0
    rejections: list[str] = field(default_factory=list)
    latency_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    prompt: str = ""
    flagged: list[str] = field(default_factory=list)  # untrusted inputs that tried to instruct

    @property
    def accepted(self) -> bool:
        return self.policy == "model"

    def policy_record(self) -> dict[str, Any]:
        """What a Decision records about the model's part."""
        out: dict[str, Any] = {"model_policy": self.policy, "prompt": self.prompt}
        if self.attempts:
            out.update(model_attempts=self.attempts, model_latency_s=round(self.latency_s, 3))
        if self.rejections:
            out["validator_rejections"] = self.rejections
        if self.flagged:
            out["untrusted_instructions_ignored"] = self.flagged
        return out


async def ask_once(
    model: BaseChatModel | None,
    function: str,
    schema: type[M],
    facts: str,
    *,
    history: list[tuple[str, str]] = (),  # (previous proposal JSON, rejection) pairs
    timeout_s: float,
    agent_id: str,
    mission_id: str | None = None,
    attempt: int = 1,
    private: bool = False,
) -> tuple[M | None, dict[str, Any]]:
    """One model call for ``function``; ``None`` if the model is absent, fails or times out.
    The stats feed the Decision and the per-call metrics."""
    prompt = load_prompt(function)
    stats: dict[str, Any] = {"prompt": prompt.ref, "latency_s": 0.0, "in": 0, "out": 0}
    if model is None or timeout_s <= 0:
        return None, stats
    messages: list[BaseMessage] = [SystemMessage(prompt.system), HumanMessage(facts)]
    for previous, rejection in history:
        messages += [
            AIMessage(previous),
            HumanMessage(
                f"The validator rejected this proposal: {rejection}. Propose again, following "
                "the rules; answer with the same JSON schema."
            ),
        ]
    started = time.perf_counter()
    # Interaction content: model-call tracing off, inputs and outputs unseen.
    hidden = tracing_context(enabled=False) if private else contextlib.nullcontext()
    try:
        token = MODEL_CALLER.set(agent_id)  # it records its own calls (below)
        try:
            with hidden:
                value, trace = await asyncio.wait_for(
                    structured_call(model, schema, messages, lambda: None),  # type: ignore[arg-type,return-value]
                    timeout_s,
                )
        finally:
            MODEL_CALLER.reset(token)
    except TimeoutError:
        value, trace = None, None
        log.info("%s %s: model timed out after %.1f s", agent_id, function, timeout_s)
    stats["latency_s"] = time.perf_counter() - started
    stats["in"] = (trace.input_tokens or 0) if trace else 0
    stats["out"] = (trace.output_tokens or 0) if trace else 0
    _record(
        agent_id,
        function,
        prompt,
        mission_id,
        attempt,
        stats["latency_s"],
        stats["in"],
        stats["out"],
        "failed" if value is None else "proposed",
    )
    return value, stats


async def propose(
    model: BaseChatModel | None,
    function: str,
    schema: type[M],
    facts: str,
    validate: Callable[[M], Any],
    *,
    timeout_s: float,
    agent_id: str,
    mission_id: str | None = None,
    untrusted_inputs: Iterable[tuple[str, str | None]] = (),
    private: bool = False,
) -> Outcome:
    """Ask, validate, one retry with the rejection, else the code decides.
    ``validate`` returns the rejection or None, possibly awaitable (it may query the
    knowledge server). ``private``: interaction content, never traced."""
    out = Outcome(prompt=load_prompt(function).ref)
    out.flagged = flag_untrusted(agent_id, function, untrusted_inputs)
    history: list[tuple[str, str]] = []
    for attempt in (1, 2):
        value, stats = await ask_once(
            model,
            function,
            schema,
            facts,
            history=history,
            timeout_s=timeout_s,
            agent_id=agent_id,
            mission_id=mission_id,
            attempt=attempt,
            private=private,
        )
        out.attempts = attempt if model is not None else 0
        out.latency_s += stats["latency_s"]
        out.input_tokens += stats["in"]
        out.output_tokens += stats["out"]
        if value is None:
            out.policy = "fallback_no_model"
            return out
        verdict = validate(value)
        if inspect.isawaitable(verdict):
            verdict = await verdict
        if verdict is None:
            out.value, out.policy = value, "model"
            return out
        out.rejections.append(verdict)
        history.append((value.model_dump_json(), verdict))
    out.policy = "fallback_rejected"
    return out


def flag_untrusted(
    agent_id: str, function: str, inputs: Iterable[tuple[str, str | None]]
) -> list[str]:
    """Labels of untrusted inputs that look like instructions (logged, never obeyed)."""
    flagged = [label for label, text in inputs if suspicious(text)]
    if flagged:
        log.warning(
            "%s %s: instruction-like untrusted input ignored: %s", agent_id, function, flagged
        )
    return flagged


def _record(
    agent_id: str,
    function: str,
    prompt: Prompt,
    mission_id: str | None,
    attempt: int,
    elapsed: float,
    tokens_in: int,
    tokens_out: int,
    outcome: str,
) -> None:
    """Rule 23: tokens and inference time per model call, apart from network metrics."""
    comms_log().record(
        {
            "ts": time.time(),
            "direction": "model",
            "agent": agent_id,
            "msg_id": None,
            "mission_id": mission_id,
            "sender": agent_id,
            "receivers": [],
            "pattern": "model",
            "performative": function,
            "schema": prompt.ref,
            "binding": ROLE,
            "attempt": attempt,
            "inference_ms": round(elapsed * 1000.0, 1),
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "outcome": outcome,
        }
    )


# --------------------------------------------------------------------------- validators


def check_deliberation(
    p: Deliberation, feasible: set[str], infeasible: set[str], pinned: set[str]
) -> str | None:
    """Rule 20 for envelope deliberation."""
    known = feasible | infeasible
    taken: set[str]
    if p.action == "accept_all":
        taken = set(feasible)
    elif p.action == "accept_subset":
        if not p.task_ids:
            return "accept_subset needs a non-empty task_ids"
        unknown = sorted(set(p.task_ids) - known)
        if unknown:
            return f"unknown task ids {unknown}"
        bad = sorted(set(p.task_ids) & infeasible)
        if bad:
            return f"cannot accept infeasible tasks {bad}"
        taken = set(p.task_ids)
        if taken != feasible and p.reason is None:
            return "leaving feasible tasks out needs a reason from the closed set"
    else:
        if p.reason is None:
            return "refuse needs a reason from the closed set"
        taken = set()
    dropped = sorted((pinned & feasible) - taken)
    if dropped:
        return f"cannot refuse pinned feasible tasks {dropped} (the operator named you)"
    if p.order and sorted(p.order) != sorted(taken):  # [] = no order proposed
        return "order must list each accepted task exactly once"
    return None


def check_replan(
    p: Replan,
    pending: list[str],
    *,
    swappable: bool,
    observations: set[str],
    max_hold_s: float,
) -> str | None:
    """Rule 20 for local replanning: nothing outside the envelope."""
    if p.action == "continue":
        return None
    if p.action == "resequence":
        if p.order is None or sorted(p.order) != sorted(pending):
            return "resequence: order must be a permutation of the pending tasks"
        return None
    if p.action in ("skip", "request_help"):
        if p.task_id not in pending:
            return f"{p.action}: {p.task_id!r} is not a pending task of this envelope"
        if p.action == "request_help" and not swappable:
            return "request_help: this envelope is not swappable"
        if p.action == "skip" and not swappable and p.reason is None:
            return "skip of a non-swappable task needs a reason from the closed set"
        return None
    if p.action == "escalate":
        if p.obs_id not in observations:
            return f"escalate: unknown observation {p.obs_id!r}"
        return None
    if p.hold_s is None or not 0 < p.hold_s <= max_hold_s:
        return f"hold: hold_s must be in (0, {max_hold_s:.0f}] s"
    return None


def check_participation(p: Participation, feasible: bool) -> str | None:
    if not p.participate and feasible and p.reason is None:
        return "declining a feasible task needs a reason from the closed set"
    return None


def check_summary(p: Summary, max_words: int) -> str | None:
    if not p.summary.strip():
        return "empty summary"
    if len(p.summary.split()) > max_words:
        return f"summary longer than {max_words} words"
    return None


class RechargeAdvice(BaseModel):
    """Early-recharge choice (agent.recharge_advisor: model)."""

    charge_now: bool
    reason: str = ""
