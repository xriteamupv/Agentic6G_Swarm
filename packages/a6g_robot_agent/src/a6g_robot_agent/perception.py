"""Perception triage.

Pipeline: on-board trigger (stub: runs after every captured inspection image;
an open-vocabulary detector or embedding diff is the intended trigger) -> on-board triage -> if
``novel or confidence < threshold`` -> edge confirmation, subject to the bandwidth budget
-> :class:`Observation`.

Implementations of :class:`Triage`:

* :class:`StubTriage` (tests): deterministic, driven by simulator-injected anomalies.
* :class:`OnboardVLMTriage`: OpenAI-compatible VLM on the dock (``onboard_vlm`` role).
* :class:`EdgeConfirmTriage`: edge VLM (``edge_vlm`` role) confirms or rejects.
"""

import base64
import hashlib
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlsplit

from a6g_common.config import ModelRole, ModelsConfig
from a6g_common.ids import IdFactory, new_id
from a6g_common.models import structured_invoke
from a6g_common.schemas import Evidence, Observation, Pose2D, TrafficClass
from a6g_transport.budget import Admission, UplinkBudget
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candidate:
    robot_id: str
    mission_id: str | None
    pose: Pose2D
    image_uri: str | None
    poi_id: str | None
    stamp: datetime


class TriageVerdict(BaseModel):
    """Structured model output (also produced by the stub)."""

    notable: bool = Field(description="anything worth reporting in the image?")
    label: str = Field("", description="short open-vocabulary label")
    novel: bool = Field(False, description="not an expected feature of this place")
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    reasoning: str = Field("", max_length=500)


class Triage(Protocol):
    model_id: str

    async def triage(self, candidate: Candidate) -> TriageVerdict: ...


class EdgeConfirm(Protocol):
    model_id: str

    async def confirm(self, candidate: Candidate, onboard: TriageVerdict) -> TriageVerdict: ...


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def image_path(uri: str | None) -> Path | None:
    if not uri:
        return None
    parts = urlsplit(uri)
    return Path(unquote(parts.path)) if parts.scheme in ("file", "") else None


def image_size(uri: str | None) -> int:
    path = image_path(uri)
    return path.stat().st_size if path and path.exists() else 0


# --------------------------------------------------------------------------- stub


class StubTriage:
    """Deterministic triage: reports whatever anomaly the simulator injected at the POI."""

    model_id = "stub-triage"
    prompt = "stub"

    def __init__(
        self, anomaly_source: Callable[[str | None], str | None], confidence: float = 0.55
    ):
        self._source = anomaly_source
        self._confidence = confidence

    async def triage(self, candidate: Candidate) -> TriageVerdict:
        label = self._source(candidate.poi_id)
        if not label:
            return TriageVerdict(notable=False)
        return TriageVerdict(
            notable=True,
            label=label,
            novel=True,
            confidence=self._confidence,
            reasoning=f"stub: injected anomaly at {candidate.poi_id}",
        )


# --------------------------------------------------------------------------- VLMs


_NO_IDENTIFICATION = (
    " If people are in view, only say that a person is present (label: person) and where; "
    "never identify anyone or describe personal attributes (face, clothing, age, gender)."
)
_ONBOARD_SYSTEM = (
    "You are the on-board perception triage of a campus security robot. Look at the "
    "image taken at a point of interest and decide whether anything notable is present "
    "(an unexpected object, person in distress, hazard, damage). Do not describe routine "
    "scenery. You never control the robot." + _NO_IDENTIFICATION
)
_EDGE_SYSTEM = (
    "You confirm or reject an observation reported by a campus robot's on-board model. "
    "Set notable=false to reject it. You never control the robot." + _NO_IDENTIFICATION
)

#: Labels that mean a person is in view (es, ca-valencia, en): code replaces them with
#: ``person`` and drops the description (no identification).
_PERSON = re.compile(
    r"\b(person|people|man|woman|men|women|child|children|kid|boy|girl|pedestrian|"
    r"visitor|student|someone|persona|personas|gente|hombre|mujer|niño|niña|peatón|"
    r"persones|home|dona|xiquet|xiqueta|vianant)\b",
    re.IGNORECASE,
)


def image_sha256(uri: str | None) -> str | None:
    path = image_path(uri)
    if path is None or not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _image_message(text: str, uri: str | None) -> HumanMessage:
    path = image_path(uri)
    if path is None or not path.exists():
        return HumanMessage(text + "\n(no image available)")
    b64 = base64.b64encode(path.read_bytes()).decode()
    return HumanMessage(
        content=[
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]
    )


def _describe(c: Candidate) -> str:
    where = f"POI {c.poi_id}" if c.poi_id else f"pose ({c.pose.x:.1f}, {c.pose.y:.1f})"
    return f"Robot {c.robot_id} at {where}."


class OnboardVLMTriage:
    def __init__(self, model: BaseChatModel, model_id: str) -> None:
        self.model, self.model_id = model, model_id
        self.prompt = _ONBOARD_SYSTEM

    async def triage(self, candidate: Candidate) -> TriageVerdict:
        verdict, fell_back = await structured_invoke(
            self.model,
            TriageVerdict,
            [SystemMessage(self.prompt), _image_message(_describe(candidate), candidate.image_uri)],
            # Model unavailable: flag for edge confirmation rather than silently ignore.
            fallback=lambda: TriageVerdict(
                notable=True,
                label="unclassified",
                novel=True,
                confidence=0.0,
                reasoning="on-board model unavailable",
            ),
        )
        if fell_back:
            log.warning("%s on-board triage fell back", candidate.robot_id)
        return verdict


class EdgeConfirmTriage:
    def __init__(self, model: BaseChatModel, model_id: str) -> None:
        self.model, self.model_id = model, model_id
        self.prompt = _EDGE_SYSTEM

    async def confirm(self, candidate: Candidate, onboard: TriageVerdict) -> TriageVerdict:
        text = (
            f"{_describe(candidate)} On-board triage says: label={onboard.label!r}, "
            f"novel={onboard.novel}, confidence={onboard.confidence:.2f}."
        )
        verdict, _ = await structured_invoke(
            self.model,
            TriageVerdict,
            [SystemMessage(self.prompt), _image_message(text, candidate.image_uri)],
            # Edge unavailable: keep the on-board verdict, but a human must confirm it.
            fallback=lambda: onboard.model_copy(update={"reasoning": "edge model unavailable"}),
        )
        return verdict


def vlm_triage(
    models: ModelsConfig, env: Mapping[str, str] | None = None
) -> tuple[OnboardVLMTriage, EdgeConfirmTriage]:
    """Both triage modes from ``models.yaml``: the on-board VLM (``onboard_vlm``,
    the dock's llama.cpp server in production) and the edge confirmation (``edge_vlm``)."""
    from a6g_common.models import chat_model

    def build(role: ModelRole) -> BaseChatModel:
        return chat_model(role, models, env)

    onboard = models.roles[ModelRole.ONBOARD_VLM].model
    edge = models.roles[ModelRole.EDGE_VLM].model
    return (
        OnboardVLMTriage(build(ModelRole.ONBOARD_VLM), onboard),
        EdgeConfirmTriage(build(ModelRole.EDGE_VLM), edge),
    )


# --------------------------------------------------------------------------- pipeline


class PerceptionPipeline:
    def __init__(
        self,
        onboard: Triage,
        edge: EdgeConfirm | None = None,
        *,
        threshold: float = 0.6,
        budget: UplinkBudget | None = None,
        ids: IdFactory = new_id,
    ) -> None:
        self.onboard, self.edge, self.threshold, self.budget, self.ids = (
            onboard,
            edge,
            threshold,
            budget,
            ids,
        )

    async def run(self, candidate: Candidate) -> Observation | None:
        verdict = await self.onboard.triage(candidate)
        if not verdict.notable:
            return None
        model_id = self.onboard.model_id
        prompt = getattr(self.onboard, "prompt", "")
        needs_edge = verdict.novel or verdict.confidence < self.threshold
        requires_confirmation = needs_edge
        if needs_edge and self.edge is not None:
            admitted = self.budget is None or (
                self.budget.admit(TrafficClass.EVENT_IMAGE, image_size(candidate.image_uri))
                is Admission.SEND
            )
            if admitted:
                confirmed = await self.edge.confirm(candidate, verdict)
                if not confirmed.notable:
                    log.info("%s observation rejected by edge VLM", candidate.robot_id)
                    return None
                verdict, model_id = confirmed, self.edge.model_id
                prompt = getattr(self.edge, "prompt", "")
                # Confirmed but still novel: a human decides (coordinator escalation).
                requires_confirmation = confirmed.novel
        person = bool(_PERSON.search(verdict.label or ""))
        return Observation(
            obs_id=self.ids("obs"),
            robot_id=candidate.robot_id,
            mission_id=candidate.mission_id,
            pose=candidate.pose,
            # The person rule: presence and place only, never a description.
            label="person" if person else (verdict.label or "unlabelled"),
            person_present=person,
            novel=verdict.novel,
            confidence=verdict.confidence,
            evidence=Evidence(
                image_uri=candidate.image_uri,
                image_sha256=image_sha256(candidate.image_uri),
                model_id=model_id,
                prompt_hash=prompt_hash(prompt),
            ),
            reasoning="" if person else verdict.reasoning,
            requires_confirmation=requires_confirmation,
            stamp=candidate.stamp,
        )
