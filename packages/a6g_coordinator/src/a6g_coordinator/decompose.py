"""Goal decomposition and chat interpretation (SPEC 10.2 ``decompose``).

The LLM turns a natural-language goal into task drafts (structured output, few-shot per
posture); ``validate`` then drops anything that does not exist on the campus. If the
model is unavailable or its output is invalid, deterministic templates are used:

* routine     -> survey every sector mentioned (all sectors if none);
* interactive -> escort to / inspect the POIs mentioned (survey if none);
* emergency   -> hold_post at every exposed POI + return_home for robots below the floor.

Task ids are deterministic (posture, kind, target) so re-stating a goal never
duplicates work.
"""

import itertools
import math
import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from a6g_common.config import CampusConfig
from a6g_common.messages import suspicious
from a6g_common.models import LLMTrace, structured_call
from a6g_common.schemas import (
    PoiTarget,
    PoseTarget,
    Posture,
    SectorTarget,
    TaskKind,
    TaskSpec,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

POSTURE_PRIORITY = {Posture.ROUTINE: 40, Posture.INTERACTIVE: 60, Posture.EMERGENCY: 90}

CAPABILITY = {
    TaskKind.SURVEY_SECTOR: ["survey"],
    TaskKind.INSPECT_POI: ["inspect"],
    TaskKind.HOLD_POST: ["hold_post"],
    TaskKind.ESCORT: ["escort"],
    TaskKind.RETURN_HOME: [],
    TaskKind.GO_TO: [],
}
DURATION_S = {
    TaskKind.SURVEY_SECTOR: 60.0,
    TaskKind.INSPECT_POI: 10.0,
    TaskKind.HOLD_POST: 600.0,
    TaskKind.ESCORT: 120.0,
    TaskKind.RETURN_HOME: 0.0,
    TaskKind.GO_TO: 0.0,
}

_EMERGENCY_WORDS = ("emergency", "weather", "rain", "storm", "alert", "evacuat", "fire")
_INTERACTIVE_WORDS = ("escort", "guide", "visitor", "accompany", "help ")
_STATUS_WORDS = ("status", "progress", "report", "how is", "what is going on", "summary")
_STOP_WORDS = (
    "stop patrol",
    "stop the patrol",
    "stop all",
    "stop the robots",
    "stop everything",
    "stop the mission",
    "cancel the mission",
    "cancel all",
    "end the mission",
    "abort the mission",
)
_PATROL = re.compile(r"\bpatrol", re.IGNORECASE)
_ALL_CLEAR_WORDS = (
    "all clear",
    "all-clear",
    "emergency is over",
    "emergency over",
    "end the emergency",
    "cancel the emergency",
    "alert is over",
    "alert has ended",
    "storm has passed",
    "stand down",
    "back to normal",
    "back to patrol",
    "patrol mode again",
    "resume patrol",
    "resume the patrol",
    "resume normal",
)
CAPABILITIES_TEXT = (
    "patrol/survey campus sectors, go to or inspect points of interest, escort a visitor to "
    "a point of interest, hold the exposed posts during an emergency, and return to base"
)
_HOLD_WORDS = ("hold", "guard", "stand watch", "keep watch")
_INSPECT_WORDS = ("inspect", "check", "look at", "photo", "picture", "examine")
_FLEET_WORDS = (
    "all the robots",
    "all of the robots",
    "all robots",
    "every robot",
    "each robot",
    "the whole fleet",
    "whole fleet",
    "the entire fleet",
)


# --------------------------------------------------------------------------- structured outputs


class TaskDraft(BaseModel):
    kind: TaskKind
    target_type: Literal["sector", "poi", "place", "staging"]
    target_id: str | None = Field(
        None, description="sector_id, poi_id or place_id; null for staging"
    )
    robot_id: str | None = Field(
        None, description="only when the operator names the robot that must do it"
    )
    reason: str = ""


class Plan(BaseModel):
    tasks: list[TaskDraft]
    #: Open tasks the goal ends ("stop holding the east gate", "stop patrolling the south
    #: plaza"), by id from the "Open tasks" list (added 2026-10-22: the plan could only add
    #: work, so "stop holding X" was answered "already under way").
    end: list[str] = Field(default_factory=list, description="ids of open tasks to end")
    summary: str = Field("", description="one sentence for the operator")


class ChatIntent(BaseModel):
    intent: Literal[
        "goal", "status", "explain", "posture", "all_clear", "stop", "unsupported", "other"
    ]
    posture: Posture | None = None
    summary: str = ""
    also_status: bool = Field(False, description="set by code: the message also asks for status")


# --------------------------------------------------------------------------- helpers


def make_task(
    kind: TaskKind,
    target: SectorTarget | PoiTarget | PoseTarget,
    posture: Posture,
    *,
    priority: int | None = None,
    suffix: str | None = None,
    depends_on: list[str] | None = None,
    duration_s: float | None = None,
) -> TaskSpec:
    if isinstance(target, SectorTarget):
        key = target.sector_id
    elif isinstance(target, PoiTarget):
        key = target.poi_id
    else:
        key = suffix or f"{target.pose.x:.0f}_{target.pose.y:.0f}"
    return TaskSpec(
        task_id=f"{posture.value[:3]}-{kind.value}-{key}",
        kind=kind,
        target=target,
        required_capabilities=CAPABILITY[kind],
        est_duration_s=DURATION_S[kind] if duration_s is None else duration_s,
        priority=POSTURE_PRIORITY[posture] if priority is None else priority,
        depends_on=depends_on or [],
        # A post is held until released, never for a set time (2026-10-21): an emergency
        # post by the all clear, any other by the next order to that robot or a stop.
        until_cleared=kind is TaskKind.HOLD_POST,
    )


def _names(campus: CampusConfig) -> list[tuple[str, str, str]]:
    """(kind, id, name) of every named place on the campus: sectors, POIs, other places."""
    return (
        [("sector", s.sector_id, s.name) for s in campus.sectors]
        + [("poi", p.poi_id, p.name) for p in campus.pois]
        + [("place", pid, name) for pid, (name, _) in campus.places().items()]
    )


def mentions(text: str, campus: CampusConfig) -> list[tuple[str, str]]:
    """(kind, id) of the places named in the text, by id or name, in text order. The
    longest name wins where names overlap: "the east gate" is the East gate, not the East
    corridor (whose short id is "east")."""
    lowered = text.lower()
    hits: list[tuple[int, int, str, str]] = []
    for kind, ident, name in _names(campus):
        short = ident.lower().split("_", 1)[-1].replace("_", " ")
        for w in {ident.lower(), short, name.lower()} - {""}:
            for m in re.finditer(rf"\b{re.escape(w)}\b", lowered):
                hits.append((m.start(), m.end(), kind, ident))
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    kept: list[tuple[int, int, str, str]] = []
    for h in hits:
        if not any(k[0] <= h[0] < k[1] or h[0] <= k[0] < h[1] for k in kept):
            kept.append(h)
    out: list[tuple[str, str]] = []
    for _, _, kind, ident in sorted(kept):
        if (kind, ident) not in out:
            out.append((kind, ident))
    return out


def mentioned(text: str, campus: CampusConfig) -> tuple[list[str], list[str]]:
    """Sector and POI ids mentioned by id or by name (case-insensitive)."""
    found = mentions(text, campus)
    order = {ident: i for i, (_, ident, _) in enumerate(_names(campus))}
    sectors = sorted((i for k, i in found if k == "sector"), key=order.__getitem__)
    pois = sorted((i for k, i in found if k == "poi"), key=order.__getitem__)
    return sectors, pois


def place_target(campus: CampusConfig, place_id: str) -> PoseTarget:
    name, pose = campus.places()[place_id]
    return PoseTarget(pose=pose, name=name)


def _target(campus: CampusConfig, kind: str, ident: str) -> SectorTarget | PoiTarget | PoseTarget:
    if kind == "sector":
        return SectorTarget(sector_id=ident)
    if kind == "poi":
        return PoiTarget(poi_id=ident)
    return place_target(campus, ident)


def _target_key(target: Any, campus: CampusConfig) -> tuple[str, str] | None:
    if isinstance(target, SectorTarget):
        return ("sector", target.sector_id)
    if isinstance(target, PoiTarget):
        return ("poi", target.poi_id)
    place = [pid for pid, (name, _) in campus.places().items() if name == target.name]
    return ("place", place[0]) if place else None


_VERBS = {
    "patrol",
    "survey",
    "inspect",
    "check",
    "escort",
    "guard",
    "hold",
    "go",
    "move",
    "stay",
    "stand",
    "watch",
    "return",
    "come",
    "drive",
    "walk",
    "take",
    "bring",
    "accompany",
    "visit",
    "monitor",
    "look",
    "keep",
    "do",
    "be",
    "help",
    "find",
    "stop",
    "wait",
}


def _words(phrase: str) -> tuple[str, ...]:
    return tuple(w for w in re.findall(r"[a-z0-9]+", phrase.lower()) if w not in _FILLER)


_GO = re.compile(r"\b(send|go|move|drive|head|dispatch)\b", re.IGNORECASE)
_FILLER = {"the", "a", "an", "of", "to", "visitor", "visitors", "please", "area", "zone"}
_DEST = re.compile(
    r"\b(?:to|towards|toward|until|up to)\s+(?:the\s+)?([^\W_][\w '\u2019-]*?)"  # accents too
    r"\s*(?=[,.;:!?]|\band\b|\bthen\b|\bwhile\b|\bplease\b|$)",
    re.IGNORECASE,
)


def _closest(phrase: str, campus: CampusConfig) -> tuple[str, str, str] | None:
    """The campus place whose name shares the most words with an unknown one ("east
    entrance" -> the East gate or the East corridor); points before sectors on a tie."""
    words = set(_words(phrase))
    best: tuple[int, int, tuple[str, str, str]] | None = None
    rank = {"poi": 0, "place": 0, "sector": 1}
    for kind, ident, name in _names(campus):
        score = len(words & set(re.findall(r"[a-z0-9]+", name.lower())))
        if score and (best is None or (score, -rank[kind]) > best[:2]):
            best = (score, -rank[kind], (kind, ident, name))
    return best[2] if best else None


def overlap_place(phrase: str, campus: CampusConfig) -> tuple[str, str, bool] | None:
    """Word overlap, the coordinator's place resolution without the knowledge server:
    (kind, id, exact) for a phrase naming a place by id or name, else the closest name
    (not exact), else None. Also the ``--kb off`` arm of the UC2-KPI-10 bench."""
    for kind, ident, name in _names(campus):
        short = ident.lower().split("_", 1)[-1].replace("_", " ")
        if _words(phrase) in {_words(w) for w in (ident.lower(), short, name.lower())}:
            return kind, ident, True
    close = _closest(phrase, campus)
    return (close[0], close[1], False) if close else None


def destination_phrases(text: str) -> list[str]:
    """The "to <place>" phrases of an order (not home, not "to inspect ...")."""
    out = []
    for m in _DEST.finditer(text):
        phrase = m.group(1).strip()
        if not phrase or any(w in f"to {phrase}".lower() for w in _HOME_WORDS):
            continue
        if phrase.lower().split()[0] in _VERBS:  # "two robots to inspect X": an order
            continue
        out.append(phrase)
    return out


#: A knowledge-server answer this sure is the place itself (no "closest place" note).
KB_EXACT = 0.95


async def kb_places(
    text: str, kb: Any, kb_log: list[str] | None = None
) -> dict[str, tuple[str, str, str, bool] | None] | None:
    """Destination phrase -> (kind, id, name, exact) from ``kb_resolve_place`` (SPEC
    A3.3.3), or None when the knowledge server cannot answer (word overlap then)."""
    if kb is None:
        return None
    out: dict[str, tuple[str, str, str, bool] | None] = {}
    try:
        for phrase in destination_phrases(text):
            found = await kb.resolve_place(phrase)
            top = found[0] if found else None
            out[phrase] = (
                (top["kind"], top["id"], top["name"], top["score"] >= KB_EXACT) if top else None
            )
    except Exception:  # unreachable, timed out: the coordinator falls back (rule 20 spirit)
        if kb_log is not None:
            kb_log.append("fallback_no_kb")
        return None
    if kb_log is not None:
        kb_log.append("kb")
    return out


def resolve_destinations(
    text: str,
    tasks: list[TaskSpec],
    pinned: dict[str, str],
    campus: CampusConfig,
    posture: Posture,
    resolved: dict[str, tuple[str, str, str, bool] | None] | None = None,
) -> tuple[list[TaskSpec], dict[str, str], list[str]]:
    """Escorts and go_to orders go where the operator said, never to a place the model
    picked instead ("escort the visitor to the east gate" became the bus stop). A place
    the coordinator does not know is replaced by the closest named one, and said so; with
    no close match the task is dropped and the operator is told which places exist."""
    moving = [t for t in tasks if t.kind in (TaskKind.ESCORT, TaskKind.GO_TO)]
    if not moving:
        return tasks, pinned, []
    notes: list[str] = []
    wanted: list[tuple[str, str]] = []
    exact = {}
    for kind, ident, name in _names(campus):
        short = ident.lower().split("_", 1)[-1].replace("_", " ")
        for w in (ident.lower(), short, name.lower()):
            exact.setdefault(_words(w), (kind, ident))
    for phrase in destination_phrases(text):
        if resolved is not None and phrase in resolved:  # the knowledge server answered
            hit = resolved[phrase]
            if hit is None:
                known = ", ".join(n for _, _, n in _names(campus))
                notes.append(
                    f"I don't know where “{phrase}” is on the campus map, so I didn't plan "
                    f"that part. Places I know: {known}."
                )
                continue
            kind, ident, name, is_exact = hit
            if not is_exact:
                notes.append(
                    f"I don't know “{phrase}” on the campus map; the closest place I know is "
                    f"the {name}, so the robot goes there instead."
                )
            wanted.append((kind, ident))
            continue
        if _words(phrase) in exact:
            wanted.append(exact[_words(phrase)])
            continue
        close = _closest(phrase, campus)
        if close is None:
            known = ", ".join(n for _, _, n in _names(campus))
            notes.append(
                f"I don't know where “{phrase}” is on the campus map, so I didn't plan "
                f"that part. Places I know: {known}."
            )
            continue
        kind, ident, name = close
        notes.append(
            f"I don't know “{phrase}” on the campus map; the closest place I know is the "
            f"{name}, so the robot goes there instead."
        )
        wanted.append((kind, ident))
    if not wanted and not notes:  # no "to <place>" phrase: any place named in the text
        wanted = mentions(text, campus)
    if not wanted and notes:  # nowhere to go
        kept = [t for t in tasks if t not in moving]
        return kept, {k: v for k, v in pinned.items() if any(t.task_id == k for t in kept)}, notes
    if not wanted:
        return tasks, pinned, notes
    out: list[TaskSpec] = []
    new_pins = dict(pinned)
    for task in tasks:
        if task in moving and _target_key(task.target, campus) not in wanted:
            kind, ident = wanted[0]
            old = _target_key(task.target, campus)
            base = make_task(task.kind, task.target, posture, suffix=old and old[1]).task_id
            tail = task.task_id.removeprefix(base) if task.task_id.startswith(base) else ""
            fixed = make_task(
                task.kind,
                _target(campus, kind, ident),
                posture,
                priority=task.priority,
                suffix=ident,
                depends_on=list(task.depends_on),
            )
            # A per-robot copy ("...-go2_02", "...-2") keeps its suffix.
            fixed = fixed.model_copy(update={"task_id": fixed.task_id + tail})
            if task.task_id in new_pins:
                new_pins[fixed.task_id] = new_pins.pop(task.task_id)
            task = fixed
        out.append(task)
    return out, new_pins, notes


def is_patrol(text: str) -> bool:
    """A patrol goes on lap after lap until stopped; a survey is one pass."""
    return bool(_PATROL.search(text))


_ANALYSE = re.compile(
    r"^(analy[sz]e|diagnose|check (whether|if|why|that)|is (this|that|it) what i|"
    r"does (this|that|it) match|what went wrong|is (this|that|it) right|"
    r"analiza|por qu[eé]|comprueba si|analitza|per qu[eè]|comprova si)",
    re.IGNORECASE,
)


def is_why(text: str) -> bool:
    """A question about the swarm's decisions (answered, and analysed with a model; step
    2, 2026-10-22: "analyse why X happened instead of Y"), never a new order."""
    lowered = text.lower().strip()
    return lowered.startswith(
        ("why", "how come", "explain", "what made", "for what reason")
    ) or bool(_ANALYSE.match(lowered))


def is_home(text: str) -> bool:
    """ "Return to base" sends robots to the staging area: an order, never a stop."""
    lowered = text.lower()
    return any(w in lowered for w in _HOME_WORDS)


_ORDER_WORDS = ("patrol", "survey", "inspect", "check", "escort", "guard", "hold", "go ", "move")


def order_after_all_clear(text: str) -> str | None:
    """The order that comes with an all-clear ("All clear, return to base"), if any."""
    rest = text.lower()
    for phrase in sorted(_ALL_CLEAR_WORDS, key=len, reverse=True):
        rest = rest.replace(phrase, " ")
    rest = re.sub(r"[^a-z0-9_ -]+", " ", rest)
    rest = re.sub(r"^(\s*(and|then|so|now|please|ok|okay))+\b", " ", rest).strip()
    if is_home(rest) or any(w in f"{rest} " for w in _ORDER_WORDS):
        return rest
    return None


def is_stop(text: str) -> bool:
    lowered = text.lower()
    return any(w in lowered for w in _STOP_WORDS)


def is_all_clear(text: str) -> bool:
    lowered = text.lower()
    return any(w in lowered for w in _ALL_CLEAR_WORDS)


def keyword_posture(text: str, default: Posture = Posture.ROUTINE) -> Posture:
    lowered = text.lower()
    if is_all_clear(lowered):
        return default  # "the emergency is over" does not raise one
    if any(w in lowered for w in _EMERGENCY_WORDS):
        return Posture.EMERGENCY
    if any(w in lowered for w in _INTERACTIVE_WORDS):
        return Posture.INTERACTIVE
    return default


def home_task(
    robot_id: str, posture: Posture, campus: CampusConfig, after: Iterable[str] = ()
) -> TaskSpec:
    staging = campus.staging
    task = make_task(TaskKind.RETURN_HOME, PoseTarget(pose=staging), posture, suffix=robot_id)
    return task.model_copy(update={"depends_on": list(after)})


_HOME_WORDS = (
    "return to base",
    "return home",
    "go home",
    "back to base",
    "go to base",
    "return to the base",
    "back home",
    "to the staging",
    "to base",  # "send the summit to base"
)


def robot_aliases(robots: Mapping[str, str]) -> dict[str, str]:
    """Words naming a robot: its id, and its platform when only one robot has it."""
    aliases = {rid.lower(): rid for rid in robots}
    by_platform: dict[str, list[str]] = {}
    for rid, platform in robots.items():
        by_platform.setdefault(platform.lower(), []).append(rid)
    for platform, ids in by_platform.items():
        if len(ids) == 1:
            aliases.setdefault(platform, ids[0])
            aliases.setdefault(platform.split("-")[0], ids[0])  # "summit-xl" -> "summit"
    return aliases


_IDLE_WORDS = (
    "the idle robots",
    "idle robots",
    "the idle ones",
    "the free robots",
    "free robots",
    "the available robots",
    "available robots",
)


def robot_groups(robots: Mapping[str, str], idle: Iterable[str] = ()) -> dict[str, list[str]]:
    """Phrases naming several robots: a platform ("the 2 Go2", "both Go2s"), the whole fleet
    ("all robots") or the robots with nothing to do ("the idle robots")."""
    by_platform: dict[str, list[str]] = {}
    for rid, platform in robots.items():
        by_platform.setdefault(platform.lower().split("-")[0], []).append(rid)
    groups: dict[str, list[str]] = {w: sorted(robots) for w in _FLEET_WORDS}
    idle_ids = sorted(r for r in idle if r in robots)
    if idle_ids:
        groups.update({w: idle_ids for w in _IDLE_WORDS})
    numbers = {2: "two", 3: "three", 4: "four"}
    for platform, ids in by_platform.items():
        if len(ids) < 2:
            continue
        n = len(ids)
        for phrase in (
            f"both {platform}s",
            f"both {platform}",
            f"all the {platform}s",
            f"all {platform}s",
            f"the {platform}s",
            f"{n} {platform}s",
            f"{n} {platform}",
            f"{numbers.get(n, n)} {platform}s",
            f"{numbers.get(n, n)} {platform}",
        ):
            groups[phrase] = sorted(ids)
    return groups


_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4}
_COUNT = re.compile(r"\b(a|an|one|two|three|four|[1-4])\s+(?:of\s+the\s+)?robots?\b", re.I)
_REST = re.compile(
    r"\bthe\s+rest(?:\s+of\s+(?:the\s+robots|them))?\b"
    r"|\b(?:the\s+)?(?:other|remaining)(?:\s+(?:one|ones|robot|robots))?\b"
    r"|\bothers\b",
    re.I,
)


def group_orders(
    text: str, robots: Mapping[str, str], idle: Iterable[str] = ()
) -> list[tuple[str, Any, str]]:
    """Who does what, in order: ("named", [robot ids], clause) for robots named by id,
    platform or group; ("count", n, clause) for "two robots ..."; ("rest", None, clause) for
    "the other(s) / the remaining robots / the rest ..."."""
    aliases: dict[str, list[str]] = {a: [r] for a, r in robot_aliases(robots).items()}
    aliases.update(robot_groups(robots, idle))
    hits: list[tuple[int, int, str, Any]] = []
    if aliases:
        words = "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))
        for m in re.finditer(rf"\b({words})\b", text, flags=re.IGNORECASE):
            hits.append((m.start(), m.end(), "named", aliases[m.group(1).lower()]))
    for m in _COUNT.finditer(text):
        word = m.group(1).lower()
        hits.append((m.start(), m.end(), "count", _NUMBERS.get(word) or int(word)))
    for m in _REST.finditer(text):
        hits.append((m.start(), m.end(), "rest", None))
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    kept: list[tuple[int, int, str, Any]] = []
    for h in hits:  # longest match first at a position; no overlaps
        if not kept or h[0] >= kept[-1][1]:
            kept.append(h)
    return [
        (kind, value, text[end : kept[i + 1][0] if i + 1 < len(kept) else len(text)])
        for i, (_start, end, kind, value) in enumerate(kept)
    ]


def robot_orders(
    text: str, robots: Mapping[str, str], idle: Iterable[str] = ()
) -> list[tuple[str, str]]:
    """Split "go2_02 return to base, the summit inspect X" into (robot_id, clause) pairs;
    a platform group ("the 2 Go2") gives the clause to each of its robots."""
    pairs = [
        (rid, clause)
        for kind, value, clause in group_orders(text, robots, idle)
        if kind == "named"
        for rid in value
    ]
    # "go2_02 and go2_01 both patrol X": a robot whose clause is only a conjunction shares
    # the next robot's order (it got nothing, 2026-10-22).
    out: list[tuple[str, str]] = []
    for i, (rid, clause) in enumerate(pairs):
        if not re.sub(r"\b(and|y|i|or|o|,|&)\b|[,&]", "", clause).strip():
            later = next((c for _, c in pairs[i + 1 :] if c.strip(" ,&")), clause)
            clause = re.sub(r"^\s*(both|ambos|tots dos)\b", "", later.strip())
        out.append((rid, clause))
    return out


def counted_orders(text: str, robots: Mapping[str, str], idle: Iterable[str] = ()) -> bool:
    """The message hands work out by count or remainder ("two robots ... the other ...")."""
    return any(kind != "named" for kind, _, _ in group_orders(text, robots, idle))


def _anonymous_plan(
    text: str,
    posture: Posture,
    campus: CampusConfig,
    robots: Mapping[str, str],
    idle: Iterable[str],
) -> tuple[list[TaskSpec], dict[str, str]]:
    """Deterministic plan for counted orders. Named robots are pinned; each counted robot,
    and each remaining one, gets the clause's tasks as one bundle ("bundle:<label>"), placed
    by the allocator on a robot of its own."""
    orders = group_orders(text, robots, idle)
    named = {r for kind, value, _ in orders if kind == "named" for r in value}
    counted = sum(value for kind, value, _ in orders if kind == "count")
    rest = max(len(robots) - len(named) - counted, 0)
    tasks: list[TaskSpec] = []
    pinned: dict[str, str] = {}
    used: set[str] = set()

    def unique_id(task: TaskSpec) -> TaskSpec:
        tid, k = task.task_id, 1
        while tid in used:
            k += 1
            tid = f"{task.task_id}-{k}"
        used.add(tid)
        return task.model_copy(update={"task_id": tid})

    for i, (kind, value, clause) in enumerate(orders):
        if kind == "named":
            for rid in value:
                for task in _clause_tasks(rid, clause, posture, campus):
                    task = unique_id(task)
                    tasks.append(task)
                    pinned[task.task_id] = rid
            continue
        copies = value if kind == "count" else rest
        planned = _clause_tasks(None, clause, posture, campus)
        if copies > 1 and planned and all(t.kind is TaskKind.SURVEY_SECTOR for t in planned):
            # "The others patrol": the sectors are shared among them (each patrolled once),
            # not one whole-campus patrol per robot (two robots on the same sectors at the
            # same time, 2026-10-21). One bundle per robot: neighbouring sectors, about the
            # same length of route each.
            for j, group in enumerate(_share_sectors(planned, min(copies, len(planned)), campus)):
                for task in group:
                    task = unique_id(task)
                    tasks.append(task)
                    pinned[task.task_id] = f"bundle:g{i}_{j + 1}"
            continue
        for k in range(copies):
            for task in _clause_tasks(None, clause, posture, campus):
                task = unique_id(task)
                tasks.append(task)
                pinned[task.task_id] = f"bundle:g{i}_{k + 1}"
    return tasks, pinned


def _share_sectors(
    surveys: list[TaskSpec], groups: int, campus: CampusConfig
) -> list[list[TaskSpec]]:
    """Split sector patrols into ``groups`` of neighbouring sectors (west to east) with
    about the same total route length."""

    def where(t: TaskSpec) -> float:
        wps = campus.sector(t.target.sector_id).survey_waypoints  # type: ignore[union-attr]
        return sum(w.x for w in wps) / len(wps) if wps else 0.0

    def length(t: TaskSpec) -> float:
        wps = campus.sector(t.target.sector_id).survey_waypoints  # type: ignore[union-attr]
        return sum(math.dist((a.x, a.y), (b.x, b.y)) for a, b in itertools.pairwise(wps)) or 1.0

    ordered = sorted(surveys, key=where)
    total = sum(length(t) for t in ordered)
    out: list[list[TaskSpec]] = [[] for _ in range(groups)]
    done, g = 0.0, 0
    for k, t in enumerate(ordered):
        left = len(ordered) - k  # leave at least one sector for each group still empty
        if out[g] and (done >= total * (g + 1) / groups or left <= groups - g - 1):
            g = min(g + 1, groups - 1)
        out[g].append(t)
        done += length(t)
    return [grp for grp in out if grp]


def _slotted(tasks: list[TaskSpec]) -> list[TaskSpec]:
    """Several robots sent to one POI in a plan get distinct spots (slot 0, 1, 2, ...)."""
    seen: dict[str, int] = {}
    out = []
    for task in tasks:
        target = task.target
        if isinstance(target, PoiTarget) and task.kind is not TaskKind.ESCORT:
            slot = seen.get(target.poi_id, 0)
            seen[target.poi_id] = slot + 1
            if slot:
                task = task.model_copy(update={"target": target.model_copy(update={"slot": slot})})
        out.append(task)
    return out


def _unique(task: TaskSpec, robot_id: str | None, pinned: dict[str, str]) -> TaskSpec | None:
    """The same task for several named robots ("the 2 Go2 inspect the bike rack") gets one
    id per robot; an exact repeat (same robot, or unnamed) is dropped."""
    if task.task_id not in pinned:
        return task
    if robot_id is None or pinned[task.task_id] == robot_id:
        return None
    return task.model_copy(update={"task_id": f"{task.task_id}-{robot_id}"})


def _clause_tasks(
    robot_id: str | None, clause: str, posture: Posture, campus: CampusConfig
) -> list[TaskSpec]:
    lowered = clause.lower()
    if any(w in lowered for w in _HOME_WORDS):
        return [home_task(robot_id, posture, campus)] if robot_id else []
    sectors, pois = mentioned(clause, campus)
    places = [i for k, i in mentions(clause, campus) if k == "place"]
    if _ESCORT.search(clause) and (pois or places):
        target = PoiTarget(poi_id=pois[0]) if pois else place_target(campus, places[0])
        return [make_task(TaskKind.ESCORT, target, posture, suffix=places[0] if places else None)]
    if not sectors and not pois and _PATROL.search(lowered):  # "patrol (the campus)"
        sectors = [s.sector_id for s in campus.sectors]
    tasks = [make_task(TaskKind.SURVEY_SECTOR, SectorTarget(sector_id=s), posture) for s in sectors]
    # "inspect / check X" takes a picture; "hold / guard X" stays there until released;
    # "send / go / move to X" only drives there.
    if any(w in lowered for w in _HOLD_WORDS):
        kind = TaskKind.HOLD_POST
    elif any(w in lowered for w in _INSPECT_WORDS):
        kind = TaskKind.INSPECT_POI
    else:
        kind = TaskKind.GO_TO
    tasks += [make_task(kind, PoiTarget(poi_id=p), posture) for p in pois]
    at_place = TaskKind.HOLD_POST if kind is TaskKind.HOLD_POST else TaskKind.GO_TO
    tasks += [make_task(at_place, place_target(campus, p), posture, suffix=p) for p in places]
    return tasks


# --------------------------------------------------------------------------- templates


def template_plan(
    text: str,
    posture: Posture,
    campus: CampusConfig,
    low_battery_robots: Iterable[str] = (),
    robots: Mapping[str, str] | None = None,
    idle: Iterable[str] = (),
) -> tuple[list[TaskSpec], dict[str, str]]:
    """Deterministic fallback. Returns (tasks, pinned task_id -> robot_id)."""
    sectors, pois = mentioned(text, campus)
    tasks: list[TaskSpec] = []
    pinned: dict[str, str] = {}
    if posture is not Posture.EMERGENCY:
        for robot_id, clause in robot_orders(text, robots or {}, idle):
            for task in _clause_tasks(robot_id, clause, posture, campus):
                task = _unique(task, robot_id, pinned)
                if task is not None:
                    tasks.append(task)
                    pinned[task.task_id] = robot_id
        if tasks:
            return tasks, pinned
        if any(w in text.lower() for w in _HOME_WORDS):  # "return to base": every robot
            for robot_id in sorted(robots or {}):
                task = home_task(robot_id, posture, campus)
                tasks.append(task)
                pinned[task.task_id] = robot_id
            if tasks:
                return tasks, pinned
    if posture is Posture.EMERGENCY:
        for poi in campus.pois:
            if poi.exposed:
                tasks.append(make_task(TaskKind.HOLD_POST, PoiTarget(poi_id=poi.poi_id), posture))
        for robot_id in sorted(low_battery_robots):
            task = home_task(robot_id, posture, campus)
            tasks.append(task)
            pinned[task.task_id] = robot_id
        return tasks, pinned
    places = [i for k, i in mentions(text, campus) if k == "place"]
    if posture is Posture.INTERACTIVE:
        if _ESCORT.search(text):
            if pois or places:
                target = PoiTarget(poi_id=pois[0]) if pois else place_target(campus, places[0])
            else:  # a place the coordinator does not know: resolve_destinations picks one
                target = PoseTarget(pose=campus.staging)
            task = make_task(TaskKind.ESCORT, target, posture, suffix=places[0] if places else None)
            return [task], pinned
        if pois:
            return [
                make_task(TaskKind.INSPECT_POI, PoiTarget(poi_id=p), posture) for p in pois
            ], pinned
    held = any(w in text.lower() for w in _HOLD_WORDS)  # "hold / guard the bike rack"
    if held and not sectors and not pois and places:  # "guard the east gate"
        return [
            make_task(TaskKind.HOLD_POST, place_target(campus, p), posture, suffix=p)
            for p in places
        ], pinned
    if not sectors and not pois and _GO.search(text) and destination_phrases(text):
        # "Send a robot to <a place not named as on the map>": a trip, never a patrol of
        # the whole campus; resolve_destinations finds the place (knowledge server, else
        # word overlap) or drops it and tells the operator.
        return [make_task(TaskKind.GO_TO, PoseTarget(pose=campus.staging), posture)], pinned
    if not sectors and not pois:
        if _unknown_object(text, campus):  # "patrol the sports hall": not on the map
            return [], pinned
        sectors = [s.sector_id for s in campus.sectors]  # "patrol", "a security round"
    elif not sectors and _PATROL.search(text):  # "patrol and check the kiosk"
        sectors = [s.sector_id for s in campus.sectors]
    # "Inspect the bike rack" is that inspection only, not a campus patrol as well.
    for sector in sectors:
        tasks.append(make_task(TaskKind.SURVEY_SECTOR, SectorTarget(sector_id=sector), posture))
    for poi in pois:
        kind = TaskKind.HOLD_POST if held else TaskKind.INSPECT_POI
        tasks.append(make_task(kind, PoiTarget(poi_id=poi), posture))
    return tasks, pinned


#: An escort said otherwise: "accompany / guide / walk / take the visitor (them) to ...".
_ESCORT = re.compile(
    r"\bescort|\b(accompany|guide|walk|take|bring|show)\b[^.]*\b(visitors?|guests?|person|"
    r"people|them|him|her)\b",
    re.IGNORECASE,
)
_OBJECT = re.compile(
    r"\b(?:patrol(?:ling)?|inspect|check|examine|look at|survey)\s+(?:the |a |an )?"
    r"([a-z0-9][a-z0-9 '-]*)",
    re.IGNORECASE,
)
_GENERIC = {
    "campus",
    "whole",
    "entire",
    "everything",
    "everywhere",
    "all",
    "area",
    "areas",
    "grounds",
    "site",
    "again",
    "mode",
    "round",
    "rounds",
    "robots",
    "robot",
    "now",
    "please",
    "continue",
    "place",
    "places",
    "around",
    "too",
    "also",
}


async def _unknown_place(text: str, campus: CampusConfig, kb: Any) -> str | None:
    """The thing an order says to patrol or inspect, when no campus place is called that
    (the knowledge server, else the names); None when it is known or there is none."""
    phrase = _object_phrase(text)
    if not phrase or not _unknown_object(text, campus):
        return None
    if kb is not None:
        try:
            return None if await kb.resolve_place(phrase) else phrase
        except Exception:  # unreachable: the names decide
            pass
    hit = overlap_place(phrase, campus)
    return None if hit and hit[2] else phrase


_OBJECT_END = re.compile(
    r"\s*(?:\band\b|\bthen\b|\bwhile\b|\bwith\b|\bbecause\b|\bsince\b|\bas\b|"
    r"\bso\b|\buntil\b|\bwhen\b|\bif\b|\bporque\b|\bya que\b|\bperqu[eè]\b|[,;.!?(]).*$",
    re.I,
)


def _object_phrase(text: str) -> str | None:
    """What the order says to patrol or inspect, up to the next clause ("inspect the bike
    rack and go2_02 patrols" -> "bike rack")."""
    m = _OBJECT.search(text)
    return _OBJECT_END.sub("", m.group(1)).strip() if m else None


def _unknown_object(text: str, campus: CampusConfig | None = None) -> bool:
    """The order names something to patrol or inspect that is not a campus place: "patrol
    the sports hall", "inspect the parking garage". A phrase naming a known place (or a
    robot) is not unknown."""
    phrase = _object_phrase(text)
    if not phrase:
        return False
    if campus is not None and mentions(phrase, campus):
        return False
    words = re.findall(r"[a-z0-9_]+", phrase.lower())
    return any(
        len(w) > 2 and w not in _GENERIC and w not in _FILLER and not _ROBOT_ID.fullmatch(w)
        for w in words
    )


_ROBOT_ID = re.compile(r"[a-z]+\d*_\d+")


# --------------------------------------------------------------------------- LLM path


def _system_prompt(campus: CampusConfig, robots: Mapping[str, str] | None = None) -> str:
    sectors = "\n".join(f"- {s.sector_id}: {s.name}" for s in campus.sectors)
    pois = "\n".join(
        f"- {p.poi_id}: {p.name} (sector {p.sector_id}"
        + (", EXPOSED to weather)" if p.exposed else ")")
        for p in campus.pois
    )
    places = "\n".join(f"- {pid}: {name}" for pid, (name, _) in campus.places().items())
    fleet = "\n".join(f"- {r}: {p}" for r, p in sorted((robots or {}).items())) or "- (unknown)"
    return f"""You plan missions for a campus security robot swarm (Go2 quadrupeds, Summit-XL).
Decompose the operator goal into robot tasks. Use ONLY these targets.

Sectors:
{sectors}
POIs:
{pois}
Places (other named spots; targets for go_to and escort only):
{places or "- (none)"}

Robots:
{fleet}

Task kinds: survey_sector (target sector), inspect_poi (target poi; go there and take a
picture), go_to (target poi or place; just drive there and stay), hold_post (target poi),
escort (target poi or place; a robot accompanies a visitor there), return_home (target
staging). Send robots only where the operator said; if that place is not listed, use the
listed one closest in name (e.g. "east entrance" -> the East gate), never another one.
You choose WHAT to do, never HOW robots move. Leave robot_id null: allocation is done by
other code, EXCEPT when the operator names the robot for a task ("go2_02 return to base",
"the summit inspects the bus stop"): then set robot_id to that robot's id. A return_home
always needs the robot_id of the robot to send back (one task per robot). When several
robots are named for the same task ("both Go2s inspect the bike rack", "all robots to the
bus stop"), output one task per robot, each with its robot_id.

Open tasks (the mission's current work) come with the goal. A goal can END some of them:
put their ids in "end" ("stop holding the east gate" -> the id of that hold_post; "stop
patrolling the south plaza" -> the id of that survey_sector). Never end work the goal does
not mention. "That robot" or "it" refers to the robot of the task the goal talks about:
use its id as robot_id for the new work ("stop holding the east gate and send that robot
to patrol the north-west walk" -> end the post, survey_sector sec_north robot_id <the
robot that held it>). A patrol of a sector already open is given to a named robot by
outputting that survey_sector with the robot_id. Output no task the goal does not ask for.

Examples:
- routine "Patrol the campus" -> survey_sector for every sector.
- routine "Check the library door and the north quad" -> inspect_poi poi_library_door,
  survey_sector sec_north.
- interactive "Escort the visitor to the bus stop" -> escort poi_bus_stop.
- emergency "Heavy rain alert" -> hold_post at every exposed POI.
- routine "go2_01 patrol the north-west walk and the summit check the bike rack" ->
  survey_sector sec_north robot_id go2_01, inspect_poi poi_bike_rack robot_id summit_01.
"""


async def decompose_goal(
    text: str,
    posture: Posture,
    campus: CampusConfig,
    model: BaseChatModel | None,
    low_battery_robots: Iterable[str] = (),
    robots: Mapping[str, str] | None = None,
    idle: Iterable[str] = (),
    kb: Any = None,
    kb_log: list[str] | None = None,
    current: list[dict[str, Any]] | None = None,
    ended: list[str] | None = None,
) -> tuple[list[TaskSpec], dict[str, str], bool, str, LLMTrace, list[str]]:
    """Returns (tasks, pinned task_id -> robot_id, used_template, summary, llm trace,
    notes for the operator).

    ``robots`` maps robot_id -> platform; tasks the operator gives to a named robot are
    pinned to it.
    """
    text, ignored = strip_injected(text)
    rest, stop_ids = stop_clauses(text, campus, current or [])
    model_ended: list[str] = []
    tasks, pinned, used_template, summary, trace = await _plan_goal(
        text,
        posture,
        campus,
        model,
        low_battery_robots,
        robots,
        idle,
        current,
        model_ended,
        template_text=rest,
    )
    if ended is not None:  # the model's ends, or the keyword rule's without a model
        ended += model_ended if not trace.used_fallback and model is not None else stop_ids
    if not rest.strip() and used_template:  # the goal only ended work
        tasks, pinned = [], {}
    resolved = None
    if any(t.kind in (TaskKind.ESCORT, TaskKind.GO_TO) for t in tasks):
        resolved = await kb_places(text, kb, kb_log)
    tasks, pinned, notes = resolve_destinations(text, tasks, pinned, campus, posture, resolved)
    unknown = await _unknown_place(text, campus, kb)
    if unknown and any(t.kind in (TaskKind.SURVEY_SECTOR, TaskKind.INSPECT_POI) for t in tasks):
        # A model stood in a known place for an unknown one ("patrol the sports hall" ->
        # the South plaza) without saying so (UC2-KPI-9): nothing is planned instead.
        known = ", ".join(n for _, _, n in _names(campus))
        notes.append(
            f"I don't know where “{unknown}” is on the campus map, so I didn't plan that. "
            f"Places I know: {known}."
        )
        tasks, pinned = [], {}
    if ignored:
        notes = [
            "I ignored instructions inside the quoted or forwarded text: "
            + "; ".join(f"“{i}”" for i in ignored)
            + ". Only your own words are orders.",
            *notes,
        ]
    return _slotted(tasks), pinned, used_template, summary, trace, notes


# --------------------------------------------------------------------------- careful path
# Expected orders keep the fast path; an order with structure the rules do not handle,
# or whose quick reading leaves something out, is planned carefully (2026-10-22).

#: Words of structure the rules do not handle (en, es, ca): counts beyond one, exclusions,
#: order, exchanges, "every", "the rest", references to a robot. Conservative: a false
#: alarm costs seconds, a miss is a misreading.
_STRUCTURE = re.compile(
    r"\b(except|excluding|other than|but not|only|then|after(?:wards)?|before|instead|"
    r"swap|switch|exchange|each|every|both|two|three|four|five|[2-9]|the rest|the others|"
    r"that robot|the same robot|another robot|the one (?:that|at|on|in|who)|"
    r"excepto|salvo|menos|s[oó]lo|solamente|luego|despu[eé]s|antes|en vez|en lugar|"
    r"intercambia|cada|todos los|todas las|ambos|dos|tres|los dem[aá]s|el resto|ese robot|"
    r"excepte|llevat|nom[eé]s|despr[eé]s|abans|en comptes|cadascun|tots els|totes les|"
    r"els altres|la resta|aquell robot)\b",
    re.IGNORECASE,
)
#: The operator says the last result was wrong, or asks for thought.
_CORRECTION = re.compile(
    r"\b(not what i|that'?s wrong|that is wrong|wrong|think (?:about|it|again|carefully)|"
    r"try again|no,|eso no|no es lo que|piensa|otra vez|aix[oò] no|no [eé]s el que|pensa)\b",
    re.IGNORECASE,
)


def is_correction(text: str) -> bool:
    """The operator says the last result was not right ("that's not what I asked")."""
    return bool(_CORRECTION.search(text))


def needs_care(text: str) -> list[str]:
    """Why the order goes to the careful path before any planning ([] = the fast path)."""
    reasons = []
    words = sorted({m.group(0).lower() for m in _STRUCTURE.finditer(text)})
    if words:
        reasons.append("structure the quick reading does not handle: " + ", ".join(words))
    if _CORRECTION.search(text):
        reasons.append("you said the last result was not right")
    return reasons


_REFERENCE = re.compile(
    r"\b(that robot|this robot|the robot|it|them|those robots|the one|the others?|the rest|"
    r"everyone|everybody|all robots|every robot|ese robot|el robot|aquell robot)\b",
    re.IGNORECASE,
)


def order_robots(text: str, robots: Mapping[str, str], idle: Iterable[str] = ()) -> set[str] | None:
    """The robots an order names (ids, platforms, groups such as "the two Go2"); None when
    it refers to robots code cannot resolve ("that robot", "the others"): no rule then."""
    if _REFERENCE.search(text):
        return None
    lowered = text.lower()
    named = {
        rid
        for alias, rid in robot_aliases(robots).items()
        if re.search(rf"\b{re.escape(alias)}\b", lowered)
    }
    for word, ids in robot_groups(robots, idle).items():
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            named |= set(ids)
    if re.search(r"\bgo2s?\b", lowered):  # "the Go2s", "the two Go2"
        named |= {r for r, p in robots.items() if p.lower().startswith("go2")}
    return named or None  # an order about places only: no rule


#: wording that takes work from whoever holds it ("go2_01 takes over the east corridor")
TAKEOVER = re.compile(
    r"\b(take|takes|taking) over\b|\b(relieve|relieves|replace|replaces|instead of|"
    r"from (the )?(summit|go2)|releva|sustituye|en lugar de|substitueix|en lloc de)",
    re.IGNORECASE,
)


def plan_gaps(
    text: str,
    tasks: list[TaskSpec],
    pinned: Mapping[str, str],
    campus: CampusConfig,
    robots: Mapping[str, str],
    ended: Iterable[str] = (),
) -> list[str]:
    """What the quick plan left out or got twice (checked by code, no model): a place the
    order names that no task targets, a robot it names that gets nothing, a sector planned
    twice. [] = the quick plan stands."""
    if any(w in text.lower() for w in _HOME_WORDS) or _STOP_CLAUSE.search(text):
        return []  # going home or stopping: no place needs a task
    gaps = []
    targeted = {_target_key(t.target, campus) for t in tasks}
    named = [
        (k, i)
        for k, i in mentions(strip_injected(text)[0], campus)
        if k in ("sector", "poi", "place")
    ]
    missing = [i for k, i in named if (k, i) not in targeted]
    if missing and (tasks or not list(ended)):
        gaps.append("the plan has nothing at " + ", ".join(missing))
    aliases = robot_aliases(robots)
    lowered = text.lower()
    said = {rid for alias, rid in aliases.items() if re.search(rf"\b{re.escape(alias)}\b", lowered)}
    idle_named = sorted(r for r in said if r not in set(pinned.values()))
    if idle_named and pinned:
        gaps.append("named robots without work: " + ", ".join(idle_named))
    sectors = [t.target.sector_id for t in tasks if isinstance(t.target, SectorTarget)]
    twice = sorted({s for s in sectors if sectors.count(s) > 1})
    if twice:
        gaps.append("sectors planned twice: " + ", ".join(twice))
    return gaps


_STOP_CLAUSE = re.compile(
    r"\bstop\s+(holding|guarding|standing guard(?:\s+at)?|keeping watch(?:\s+at)?|"
    r"patrolling|inspecting|checking)\b(?P<what>[^,.;]*?)(?=\s+and\b|\s+then\b|[,.;]|$)",
    re.IGNORECASE,
)


def stop_clauses(
    text: str, campus: CampusConfig, current: list[dict[str, Any]]
) -> tuple[str, list[str]]:
    """Without a model, "stop holding / guarding / patrolling X" ends the open tasks of
    that kind at X (all of that kind when no place is named). Returns the goal without
    those clauses and the ids to end."""
    kinds = {
        "holding": "hold_post",
        "guarding": "hold_post",
        "standing": "hold_post",
        "keeping": "hold_post",
        "patrolling": "survey_sector",
        "inspecting": "inspect_poi",
        "checking": "inspect_poi",
    }
    ended: list[str] = []
    for m in _STOP_CLAUSE.finditer(text):
        kind = kinds[m.group(1).split()[0].lower()]
        targets = {i for _, i in mentions(m.group("what"), campus)}
        ended += [
            c["task_id"]
            for c in current
            if c["kind"] == kind
            and (not targets or c["target_id"] in targets)
            and c["task_id"] not in ended
        ]
    rest = _STOP_CLAUSE.sub("", text)
    rest = re.sub(r"^\W*(and|then)\b", "", rest.strip(), flags=re.IGNORECASE).strip(" ,.;")
    return rest, ended


#: Instruction markers no operator types in an order: pasted or forwarded content.
_INJECTED = re.compile(
    r"\b(ignore|disregard|override)\b.{0,40}\b(instructions?|rules?|envelope|prompt)\b"
    r"|\byou (must|should) (now )?(accept|refuse|obey)\b|\bsystem\s*:|\bnew instructions?\b",
    re.IGNORECASE,
)
#: Reported speech: what follows is someone else's text, not the operator's.
_REPORTED = re.compile(
    r"\b(says?|said|reads?|message|note|forwarded|notice|wrote|written|asked)\b", re.IGNORECASE
)
_QUOTED = re.compile(r"[\"“«]([^\"”»]*)[\"”»]")
_CLAUSES = re.compile(r"(?<=[.;:!?])\s+|\s*[()]\s*")


def strip_injected(text: str) -> tuple[str, list[str]]:
    """Instructions inside quoted or forwarded text are data, never orders (rule 24): drop
    quoted spans that read as instructions, clauses with an injection marker ("system:",
    "ignore previous instructions"), and instruction-like clauses in reported speech ("a
    note says: ..."). The operator's own words stay, even "forget the previous order"."""
    if not suspicious(text):
        return text, []
    ignored: list[str] = []

    def quoted(m: re.Match) -> str:
        if suspicious(m.group(1)):
            ignored.append(m.group(1).strip())
            return ""
        return m.group(0)

    out = _QUOTED.sub(quoted, text)
    reported = bool(_REPORTED.search(text))
    kept = []
    for part in _CLAUSES.split(out):
        part = part.strip()
        if part and (_INJECTED.search(part) or (reported and suspicious(part))):
            ignored.append(part.rstrip(".;: "))
        elif part:
            kept.append(part)
    return " ".join(kept), ignored


async def _plan_goal(
    text: str,
    posture: Posture,
    campus: CampusConfig,
    model: BaseChatModel | None,
    low_battery_robots: Iterable[str],
    robots: Mapping[str, str] | None,
    idle: Iterable[str],
    current: list[dict[str, Any]] | None = None,
    ended: list[str] | None = None,
    template_text: str | None = None,
) -> tuple[list[TaskSpec], dict[str, str], bool, str, LLMTrace]:
    """``current``: the mission's open tasks (shown to the model); ``ended``: filled with
    the ids of those the model's plan ends; ``template_text``: the goal for the rule-based
    paths, without the clauses that only end work."""
    robots = dict(robots or {})
    idle = list(idle)
    full_text, text = text, template_text if template_text is not None else text
    if posture is not Posture.EMERGENCY and counted_orders(text, robots, idle):
        # "Two robots inspect X and the other patrols": counts and remainders are parsed by
        # code (the model's plans for these varied from run to run).
        tasks, pins = _anonymous_plan(text, posture, campus, robots, idle)
        if tasks:
            return _slotted(tasks), pins, True, "per-robot plan (counted orders)", LLMTrace()
    template_tasks, pinned = template_plan(text, posture, campus, low_battery_robots, robots, idle)

    def fallback() -> Plan:
        return Plan(tasks=[], summary="template")

    plan, trace = await structured_call(
        model,
        Plan,
        [
            SystemMessage(_system_prompt(campus, robots)),
            HumanMessage(
                f"Posture: {posture.value}. Goal: {full_text}\nOpen tasks:\n"
                + (
                    "\n".join(
                        f"- {c['task_id']}: {c['kind']} {c['target_id']} "
                        f"(robot {c.get('robot_id') or 'none'}, {c['status']})"
                        for c in current or []
                    )
                    or "- (none)"
                )
            ),
        ],
        fallback,
        visible=True,  # a reasoning model's thoughts stream to the chat
    )
    if not trace.used_fallback and plan.end and ended is not None:
        known = {c["task_id"] for c in current or []}
        ended += [tid for tid in plan.end if tid in known and tid not in ended]
        if not plan.tasks:  # it only ends work: no template plan behind it
            return [], {}, False, plan.summary or "end tasks", trace
    if trace.used_fallback or not plan.tasks:
        summary = f"template plan for posture {posture.value}"
        return _slotted(template_tasks), pinned, True, summary, trace
    tasks: list[TaskSpec] = []
    llm_pinned: dict[str, str] = {}
    seen: set[str] = set()
    # Robots the operator named (id, platform, group) are pinned. A robot the model picked on
    # its own ("and other to patrol the campus" -> go2_01) only means "these tasks go to ONE
    # robot": a bundle, "bundle:<label>" in the returned mapping, for the allocator to place.
    orders = robot_orders(text, robots, idle)
    named = {rid for rid, _ in orders}
    # "Send the summit to base and go2_02 to ...": a return_home the model left without a
    # robot is for the robots told to go home, not the whole fleet.
    sent_home = sorted({rid for rid, clause in orders if is_home(clause)}) if orders else None
    copies: dict[str, int] = {}
    for draft in plan.tasks:
        robot = draft.robot_id if draft.robot_id in named else None
        if draft.kind is TaskKind.RETURN_HOME:
            if robot:
                homes = [robot]
            elif sent_home is not None:
                homes = sent_home
            else:  # "Return to base": everyone
                homes = sorted(robots)
            for home_robot in homes:
                task = home_task(home_robot, posture, campus)
                if task.task_id not in llm_pinned:
                    tasks.append(task)
                    llm_pinned[task.task_id] = home_robot
            continue
        else:
            if draft.target_type == "sector" and draft.target_id:
                target = SectorTarget(sector_id=draft.target_id)
            elif draft.target_type == "poi" and draft.target_id:
                target = PoiTarget(poi_id=draft.target_id)
            elif draft.target_type == "place" and draft.target_id in campus.places():
                target = place_target(campus, draft.target_id)
            else:
                target = PoseTarget(pose=campus.staging)
            # Priority comes from the posture, never the model: envelope priorities are
            # compared across missions (a model's "1 = first" made robots decline).
            task = make_task(draft.kind, target, posture, suffix=draft.target_id)
        owner = llm_pinned.get(task.task_id)
        if task.task_id in seen and robot is not None and owner == robot:
            continue  # an exact repeat
        if task.task_id in seen and robot is not None:  # the same task, another named robot
            task = task.model_copy(update={"task_id": f"{task.task_id}-{robot}"})
        elif task.task_id in seen:  # "two robots inspect X": another copy for the allocator
            if task.kind in (TaskKind.SURVEY_SECTOR, TaskKind.RETURN_HOME):
                continue  # a sector patrolled twice at once is a repeat, not a request
            copies[task.task_id] = copies.get(task.task_id, 1) + 1
            task = task.model_copy(update={"task_id": f"{task.task_id}-{copies[task.task_id]}"})
        seen.add(task.task_id)
        tasks.append(task)
        if robot:
            llm_pinned[task.task_id] = robot
        elif (
            draft.robot_id in robots
            and task.kind is not TaskKind.HOLD_POST
            and posture is not Posture.EMERGENCY
        ):
            # Never for posts (one per robot: a bundle would queue two posts on one robot
            # while the other is uncovered) nor in an emergency (the allocator places posts).
            llm_pinned[task.task_id] = f"bundle:{draft.robot_id}"
    if posture is Posture.EMERGENCY:  # safety rule is not left to the model
        for task in template_tasks:
            if task.task_id in pinned:
                tasks.append(task)
                llm_pinned[task.task_id] = pinned[task.task_id]
    real = {r for r in llm_pinned.values() if not r.startswith("bundle:")}
    if pinned and not set(pinned.values()) <= real and posture is not Posture.EMERGENCY:
        # The operator named robots ("all robots", "the 2 Go2", "the idle robots") but the
        # model did not give them their tasks: the deterministic per-robot plan wins.
        return _slotted(template_tasks), pinned, True, "per-robot template plan", trace
    return _slotted(tasks), llm_pinned, False, plan.summary or "LLM plan", trace


async def interpret_chat(
    text: str, has_mission: bool, model: BaseChatModel | None
) -> tuple[ChatIntent, LLMTrace]:
    lowered = text.lower()

    def fallback() -> ChatIntent:
        if has_mission and is_why(lowered):
            return ChatIntent(intent="explain")
        if has_mission and is_all_clear(lowered):
            return ChatIntent(intent="all_clear", posture=Posture.ROUTINE)
        if is_home(lowered):
            return ChatIntent(intent="goal", posture=Posture.ROUTINE)
        if has_mission and is_stop(lowered):
            return ChatIntent(intent="stop")
        # With no mission a short status question ("status", "what is going on?") is still
        # one: it started a patrol, the planner's default for an order it cannot read. A
        # longer text ("inspect the kiosk and report back") stays an order.
        short = len(re.findall(r"\w+", lowered)) <= 4
        if (has_mission or short) and any(w in lowered for w in _STATUS_WORDS):
            return ChatIntent(intent="status")
        posture = keyword_posture(text)
        if has_mission and posture is Posture.EMERGENCY:
            return ChatIntent(intent="posture", posture=posture)
        return ChatIntent(intent="goal", posture=posture)

    intent, trace = await structured_call(
        model,
        ChatIntent,
        [
            SystemMessage(
                "Classify an operator message to a robot-swarm coordinator. intent=goal for a "
                "new task request, status for a progress question, explain for a why/how question "
                "about the coordinator's or a robot's decisions, posture for a change of "
                "situation (e.g. weather alert -> emergency), all_clear when the operator ends "
                "an emergency or asks to go back to normal operation / patrol mode, stop when "
                "the operator wants the robots to stop their work (stop patrolling, cancel "
                "the mission), "
                "unsupported for a request the robots cannot do (put a one-sentence reason "
                "in summary), other otherwise. The robots can only: "
                + CAPABILITIES_TEXT
                + ". Postures: routine, interactive (visitor help/escort), emergency."
                + (" A mission is already running." if has_mission else "")
            ),
            HumanMessage(text),
        ],
        fallback,
        visible=True,
    )
    if is_why(lowered) and has_mission:  # a question about decisions, never a new order
        intent.intent = "explain"
    elif is_all_clear(lowered) and has_mission:
        intent.intent, intent.posture = "all_clear", Posture.ROUTINE
    elif intent.intent == "unsupported" and re.search(
        r"\b(send|go|move|drive|take)\b.*\bto\b", lowered
    ):  # "send all robots to the bus stop": a go_to order
        intent.intent = "goal"
        intent.posture = intent.posture or keyword_posture(text)
    elif is_home(lowered):  # an order (go to the staging area), never "stop"
        intent.intent = "goal"
        intent.posture = intent.posture or Posture.ROUTINE
    elif is_stop(lowered) and has_mission:
        intent.intent = "stop"
    elif intent.intent in ("all_clear", "stop") and not has_mission:
        intent.intent = "other"
    if intent.intent in ("goal", "posture") and intent.posture is None:
        intent.posture = keyword_posture(text)
    # Safety: an emergency in the text is never downgraded by the model (SPEC Rule 1).
    if keyword_posture(text) is Posture.EMERGENCY and intent.intent not in ("status", "stop"):
        intent.posture = Posture.EMERGENCY
        if has_mission:
            intent.intent = "posture"
    intent.also_status = intent.intent != "status" and any(w in lowered for w in _STATUS_WORDS)
    return intent, trace
