"""Constraint-aware guidance on the robot and its forgetting.

What a person says, their declared constraint and the routing profile derived from it live
only in the InteractionStore and in this robot's in-memory copy (``deps.local``), never in
the graph state, the checkpoint, the decision log or the reports. The graph carries the
``interaction_id``; decisions hold ids, POI ids, the ``route_id``, timestamps and booleans.

The model proposes (objective as said, declared constraint, profile); code resolves the
place through the knowledge server, maps the constraint to a profile (never ``default``
when one was declared), plans routes and checks their compliance.
"""

import contextlib
import logging
import math
import re
from datetime import datetime
from typing import Any, Literal

from a6g_common.routegraph import DEFAULT_PROFILES, RoutingProfile
from a6g_common.schemas import PoiTarget, PoseTarget, SectorTarget, TaskSpec
from a6g_interactions import InteractionDenied, InteractionNotFound, StoreUnavailable
from a6g_interactions import language as lang_
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

#: Within this of the meeting point the robot is with the person (hand-over approach).
MEET_RADIUS_M = 3.0


class Request(BaseModel):
    """What a person asked for: the model's proposal, checked by code."""

    objective_kind: Literal["escort", "none"]
    objective: str | None = Field(None, description="the place, as the person said it")
    declared_constraint: str | None = Field(None, description="in the person's words")
    profile: str | None = Field(None, description="one of the people routing profiles")


def profiles(campus: Any) -> dict[str, RoutingProfile]:
    return {**DEFAULT_PROFILES, **campus.people_routing_profiles}


def place_names(campus: Any) -> dict[str, tuple[str, str]]:
    """Display name (lower case) -> (kind, id), for the local fallback and the prompt."""
    out: dict[str, tuple[str, str]] = {}
    for s in campus.sectors:
        for n in (s.name, *s.aliases):
            if n:
                out[n.lower()] = ("sector", s.sector_id)
    for p in campus.pois:
        for n in (p.name, *p.aliases):
            if n:
                out[n.lower()] = ("poi", p.poi_id)
    for r in campus.link_recovery_points:
        for n in (r.label, *r.aliases):
            if n:
                out[n.lower()] = ("place", r.point_id)
    return out


def display_name(campus: Any, kind: str, ident: str) -> str:
    if kind == "poi":
        return campus.poi(ident).name or ident
    if kind == "sector":
        return campus.sector(ident).name or ident
    return campus.places()[ident][0]


async def resolve(deps: Any, phrase: str, mission_id: str | None) -> dict[str, str] | None:
    """The campus place for ``phrase``: the knowledge server, else the names."""
    phrase = (phrase or "").strip()
    if not phrase:
        return None
    if deps.kb is not None:
        try:
            found = await deps.kb.resolve_place(phrase, mission_id)
        except Exception:
            found = None
        if found:
            top = found[0]
            return {"kind": top["kind"], "id": top["id"], "said": top["matched"]}
        if found is not None:
            return None  # the server answered: no such place
    lowered = lang_._norm(phrase)
    for name in sorted(place_names(deps.campus), key=len, reverse=True):
        if re.search(rf"\b{re.escape(lang_._norm(name))}\b", lowered):
            kind, ident = place_names(deps.campus)[name]
            return {"kind": kind, "id": ident, "said": name}
    return None


def check_request(p: Request, utterance: str, closed: set[str]) -> str | None:
    """Rule 20 for the request (place resolution is checked separately, async)."""
    if p.objective_kind == "escort" and not (p.objective or "").strip():
        return "an escort needs the objective: the place as the person said it"
    if lang_.stated_constraint(utterance) and not (p.declared_constraint or "").strip():
        return "the person stated a constraint: declared_constraint must carry it"
    if p.profile is not None and p.profile not in closed:
        return f"profile must be one of {sorted(closed)}"
    return None


def fallback_request(utterance: str) -> Request:
    """Deterministic reading: the whole utterance is the objective."""
    return Request(
        objective_kind="escort",
        objective=utterance,
        declared_constraint=lang_.stated_constraint(utterance),
    )


def goal_ref(deps: Any, dest: dict[str, str]) -> Any:
    """What ``plan_route`` takes for a destination: a POI id or a pose."""
    if dest["kind"] == "poi":
        return dest["id"]
    if dest["kind"] == "place":
        pose = deps.campus.places()[dest["id"]][1]
        return {"x": pose.x, "y": pose.y}
    sector = deps.campus.sector(dest["id"])
    if sector.survey_waypoints:
        w = sector.survey_waypoints[0]
        return {"x": w.x, "y": w.y}
    xs, ys = zip(*sector.polygon, strict=True)
    return {"x": sum(xs) / len(xs), "y": sum(ys) / len(ys)}


def dest_of(spec: TaskSpec) -> dict[str, str] | None:
    t = spec.target
    if isinstance(t, PoiTarget):
        return {"kind": "poi", "id": t.poi_id}
    if isinstance(t, SectorTarget):
        return {"kind": "sector", "id": t.sector_id}
    if isinstance(t, PoseTarget):
        return {"kind": "pose", "x": t.pose.x, "y": t.pose.y}  # type: ignore[dict-item]
    return None


# --------------------------------------------------------------------------- the entry


async def fetch(deps: Any, iid: str) -> tuple[dict[str, Any] | None, str]:
    """The interaction (store first, the robot's copy as backup): (entry, how).

    how: ``store`` | ``restored`` (the store lost it; re-created from the copy) |
    ``local`` (store unreachable) | ``lost`` (neither has it)."""
    local = deps.local.get(iid)
    if deps.interactions is None:
        return (local, "local") if local else (None, "lost")
    try:
        entry = await deps.interactions.get(iid)
    except InteractionNotFound:
        if local is None:
            return None, "lost"
        try:
            entry = await deps.interactions.restore(_view(local))
        except (StoreUnavailable, InteractionDenied, ValueError):
            return local, "local"
        _merge(deps, iid, entry)
        return deps.local[iid], "restored"
    except (StoreUnavailable, InteractionDenied):
        return (local, "local") if local else (None, "lost")
    _merge(deps, iid, entry)
    return deps.local[iid], "store"


def _view(local: dict[str, Any]) -> dict[str, Any]:
    return {k: local[k] for k in ("interaction_id", "participants", "created", "content")}


def _merge(deps: Any, iid: str, entry: dict[str, Any]) -> None:
    local = deps.local.setdefault(iid, {"stage": "escorting"})
    content = {**(local.get("content") or {}), **(entry.get("content") or {})}
    local.update(
        interaction_id=iid,
        participants=list(entry.get("participants") or [deps.robot_id]),
        created=entry.get("created") or local.get("created"),
        content=content,
    )


async def note(deps: Any, iid: str, **fields: Any) -> None:
    """Record ``fields`` in the copy and, best effort, in the store (timestamps, stage)."""
    local = deps.local.get(iid)
    if local is not None:
        local.setdefault("content", {}).update(fields)
    if deps.interactions is not None:
        with contextlib.suppress(Exception):  # the copy has it; a restore carries it
            await deps.interactions.update(iid, content=fields)


async def release(deps: Any, iid: str, **fields: Any) -> dict[str, Any] | None:
    """Leave an interaction to another robot: the store must have it for the
    next robot, so a lost entry is re-created from this robot's copy first (otherwise the
    hand-over loses the context); then record ``fields`` and drop the copy."""
    if deps.interactions is not None and iid in deps.local:
        with contextlib.suppress(Exception):
            await fetch(deps, iid)  # restores from the copy when the store lost it
    await note(deps, iid, **fields)
    return deps.local.pop(iid, None)


def now_iso(deps: Any) -> str:
    return deps.clock.now().isoformat()


def seconds_between(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    return round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds(), 3)


def metrics(content: dict[str, Any]) -> dict[str, Any]:
    """Guidance metrics: timestamps and booleans only, no personal data."""
    return {
        "request_to_confirmation_s": seconds_between(
            content.get("t_request"), content.get("t_confirmed")
        ),
        "request_to_departure_s": seconds_between(
            content.get("t_request"), content.get("t_departure")
        ),
        "p7_round_trip_s": seconds_between(content.get("t_confirmed"), content.get("t_envelope")),
        "route_compliant": content.get("route_compliant"),
        "handovers": int(content.get("handovers") or 0),
        "repeated_requests": int(content.get("repeated_requests") or 0),
    }


def distance(a: dict[str, Any], b: dict[str, Any]) -> float:
    return math.hypot(float(a["x"]) - float(b["x"]), float(a["y"]) - float(b["y"]))


def route_elements(route: dict[str, Any], from_leg: int) -> set[str]:
    """Edges (and their end nodes) of the route from leg ``from_leg`` on."""
    edges = {e for leg in route.get("leg_edges", [])[from_leg:] for e in leg}
    nodes = {n for e in edges for n in route.get("edge_nodes", {}).get(e, [])}
    if route.get("goal_node"):
        nodes.add(route["goal_node"])
    return edges | nodes | {n.removeprefix("poi:") for n in nodes}
