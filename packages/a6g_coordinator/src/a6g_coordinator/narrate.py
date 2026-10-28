"""Operator-facing messages in plain language (agent-chat-ui, ``a6g goal``).

Messages are written by deterministic templates from the campus's own names ("patrol the
North quad", "stand guard at the Bike rack"), so they never state anything the
coordinator did not decide. Task ids, costs and policies stay in the decision log.

Optionally (``coordinator.narration: llm``) the model rephrases a message; the rewrite is
only used if it still mentions every robot the template mentions, otherwise the template
text is kept.
"""

import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any

from a6g_common.clock import time_flows
from a6g_common.config import CampusConfig
from a6g_common.schemas import PoiTarget, PoseTarget, SectorTarget, TaskKind, TaskSpec
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

log = logging.getLogger(__name__)

PLATFORM = {"go2": "Go2", "summit": "Summit-XL"}
STATUS = {
    "pending": "waiting for a robot",
    "assigned": "queued",
    "in_progress": "in progress",
    "done": "done",
    "preempted": "paused for the emergency",
    "failed": "failed",
    "dropped": "dropped",
}
ROBOT_STATUS = {
    "available": "available",
    "lost": "out of contact (probably a coverage shadow)",
    "failed": "down",
}
POSTURE = {
    "routine": "routine",
    "interactive": "interactive (visitor assistance)",
    "emergency": "emergency",
}


def join(items: Iterable[str]) -> str:
    items = list(items)
    if not items:
        return "nothing"
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


class Narrator:
    def __init__(self, campus: CampusConfig) -> None:
        self.campus = campus

    # ------------------------------------------------------------------ names

    def place(self, spec: TaskSpec) -> str:
        t = spec.target
        if isinstance(t, SectorTarget):
            sector = next((s for s in self.campus.sectors if s.sector_id == t.sector_id), None)
            return f"the {sector.name}" if sector and sector.name else t.sector_id
        if isinstance(t, PoiTarget):
            poi = next((p for p in self.campus.pois if p.poi_id == t.poi_id), None)
            return f"the {poi.name.lower()}" if poi and poi.name else t.poi_id
        if isinstance(t, PoseTarget):
            if t.name:
                return f"the {t.name.lower()}"
            s = self.campus.staging
            if abs(t.pose.x - s.x) < 1 and abs(t.pose.y - s.y) < 1:
                return "base"
            return f"the spot at ({t.pose.x:.0f}, {t.pose.y:.0f})"
        return "?"

    def task(self, spec: TaskSpec) -> str:
        text = self._task(spec)
        if spec.constraints:  # declared by the person: shown wherever the task is named
            text += " (" + "; ".join(spec.constraints) + ")"
        return text

    def _task(self, spec: TaskSpec) -> str:
        where = self.place(spec)
        return {
            TaskKind.SURVEY_SECTOR: f"patrol {where}",
            TaskKind.INSPECT_POI: f"check {where}",
            TaskKind.HOLD_POST: f"stand guard at {where}",
            TaskKind.ESCORT: f"escort the visitor to {where}",
            TaskKind.RETURN_HOME: "return to base",
            TaskKind.GO_TO: f"go to {where}",
        }[spec.kind]

    def task_done(self, spec: TaskSpec) -> str:
        """Completion phrase: '... finished patrolling the North quad'."""
        where = self.place(spec)
        return {
            TaskKind.SURVEY_SECTOR: f"finished patrolling {where}",
            TaskKind.INSPECT_POI: f"finished checking {where}",
            TaskKind.HOLD_POST: f"finished standing guard at {where}",
            TaskKind.ESCORT: f"brought the visitor to {where}",
            TaskKind.RETURN_HOME: "is back at base",
            TaskKind.GO_TO: f"reached {where}",
        }[spec.kind]

    def task_id(self, tasks: Mapping[str, dict[str, Any]], task_id: str) -> str:
        entry = tasks.get(task_id)
        return self.task(TaskSpec.model_validate(entry["spec"])) if entry else task_id

    @staticmethod
    def robot(robot_id: str, fleet: Mapping[str, dict[str, Any]] | None = None) -> str:
        entry = (fleet or {}).get(robot_id)
        platform = PLATFORM.get(entry["platform"], "") if entry else ""
        return f"**{robot_id}**" + (f" ({platform})" if platform else "")

    # ------------------------------------------------------------------ messages

    def plan(
        self,
        posture: str,
        specs: list[TaskSpec],
        skipped: int = 0,
        covered: list[str] | None = None,
    ) -> str:
        if not specs:
            if covered:
                return f"That is already under way: {join(covered)}. Nothing new to add."
            return "I found nothing to plan for that request; nothing was changed."
        kind = {
            "routine": "Routine mission",
            "interactive": "Visitor request",
            "emergency": "Emergency",
        }
        label = kind.get(posture, posture.capitalize())
        head = f"{label}: I've planned {plural(len(specs), 'task')}: "
        text = head + join(self.task(s) for s in specs) + "."
        if skipped:
            text += f" ({plural(skipped, 'request')} already in progress, not duplicated.)"
        return text

    def allocation(
        self,
        by_robot: Mapping[str, list[str]],
        tasks: Mapping[str, dict[str, Any]],
        fleet: Mapping[str, dict[str, Any]],
        *,
        replanned: bool = False,
    ) -> str:
        lines = ["Updated assignments:" if replanned else "Assignments:"]
        for rid, ids in sorted(by_robot.items()):
            steps = [self.task_id(tasks, t) for t in ids]
            first, rest = steps[0], steps[1:]
            line = f"- {self.robot(rid, fleet)} will {first}"
            if rest:
                line += ", then " + join(rest)
            # What the robot already has: the new work queues behind it (the robot orders
            # its own queue; otherwise "go2_02 will patrol the South plaza" reads as its
            # current task while it patrols the North-west walk).
            now = [
                self.task_id(tasks, t)
                for t, e in sorted(tasks.items())
                if t not in ids and e.get("robot_id") == rid and e.get("status") == "in_progress"
            ]
            queued = [
                self.task_id(tasks, t)
                for t, e in sorted(tasks.items())
                if t not in ids and e.get("robot_id") == rid and e.get("status") == "assigned"
            ]
            if now or queued:
                busy = [f"now: {join(now)}"] if now else []
                busy += [f"already queued: {join(queued)}"] if queued else []
                line += " next (" + "; ".join(busy) + ")"
            lines.append(line + ".")
        return "\n".join(lines)

    def unallocated(
        self,
        items: Mapping[str, str],
        tasks: Mapping[str, dict[str, Any]],
        details: Mapping[str, str] | None = None,
        away: list[str] | None = None,
        fleet: Mapping[str, dict[str, Any]] | None = None,
    ) -> str:
        why = {
            "battery": "no robot has enough battery for it right now: each keeps a reserve, "
            "and I'll assign it once one has recharged",
            "capability": "no robot can do it",
            "region": "it is outside every robot's area",
            "conflict": "every available robot is busy or declined",
        }
        parts = []
        for t, r in items.items():
            reason = why.get(r, r)
            if r == "conflict" and "already holds a post" in (details or {}).get(t, ""):
                reason = "every available robot is already holding a post"
            robot = (tasks.get(t) or {}).get("pinned_to")
            if r == "battery" and robot:  # work given by name: say whose battery
                end = re.search(r"would end at ([\d.]+)%", (details or {}).get(t, ""))
                reason = (
                    f"{self.robot(robot, fleet or {})} hasn't enough battery for it"
                    + (
                        f": it would end at {float(end.group(1)):.0f} %, below its reserve"
                        if end
                        else ""
                    )
                    + f"; I'll give it to {robot} once it has recharged"
                )
            parts.append(f"{self.task_id(tasks, t)} ({reason})")
        text = "I couldn't assign: " + join(parts) + "."
        if all(r == "battery" for r in items.values()):
            return text  # it says when: once a robot has recharged
        if away and any(r == "conflict" for r in items.values()):
            verb = "is" if len(away) == 1 else "are"
            return text + f" I'll assign it as soon as {join(away)} {verb} back in contact."
        return text + " I'll retry when the fleet changes."

    def status(self, state: Mapping[str, Any]) -> str:
        tasks: Mapping[str, dict[str, Any]] = state.get("tasks") or {}
        fleet: Mapping[str, dict[str, Any]] = state.get("fleet") or {}
        if not tasks:
            return "No mission is running yet. Tell me what to do, for example *Patrol the campus*."
        posture = POSTURE.get(state.get("posture") or "", state.get("posture") or "")
        if state.get("status") == "completed":
            out = [f"Everything I was asked to do is done (we're in **{posture}** mode)."]
        else:
            out = [f"The mission is under way in **{posture}** mode."]
        robot_lines = []
        for rid in sorted(fleet):
            mine = [t for t in sorted(tasks) if tasks[t].get("robot_id") == rid]
            doing = [self.task_id(tasks, t) for t in mine if tasks[t]["status"] == "in_progress"]
            queued = [self.task_id(tasks, t) for t in mine if tasks[t]["status"] == "assigned"]
            health = ROBOT_STATUS.get(fleet[rid]["status"], fleet[rid]["status"])
            line = f"- {self.robot(rid, fleet)}: "
            if fleet[rid]["status"] != "available":
                line += health
                if doing:
                    line += f"; last seen working to {join(doing)}"
            elif doing:
                line += f"working to {join(doing)}"
            elif queued:
                line += "about to start"
            else:
                line += "idle"
            if queued:
                line += f"; next: {join(queued)}"
            robot_lines.append(line + ".")
        out.append("\n".join(robot_lines))
        done = [t for t in sorted(tasks) if tasks[t]["status"] == "done"]
        waiting = [t for t in sorted(tasks) if tasks[t]["status"] == "pending"]
        paused = [t for t in sorted(tasks) if tasks[t]["status"] == "preempted"]
        if done:
            out.append(
                f"Done so far ({len(done)}): " + join(self.task_id(tasks, t) for t in done) + "."
            )
        if waiting:
            out.append(
                "Waiting for a robot: " + join(self.task_id(tasks, t) for t in waiting) + "."
            )
        if paused:
            out.append(
                "Paused for the emergency: " + join(self.task_id(tasks, t) for t in paused) + "."
            )
        return "\n\n".join(out)


async def polish(model: BaseChatModel | None, text: str) -> str:
    """Optional LLM rephrasing; falls back to ``text`` unless every robot id survives."""
    if model is None or not text.strip():
        return text
    robots = set(re.findall(r"\*\*([a-z][a-z0-9_]*)\*\*", text))
    try:
        async with time_flows():  # the world does not wait for a model
            reply = await model.ainvoke(
                [
                    SystemMessage(
                        "You rephrase status messages from a robot-swarm coordinator for a "
                        "security operator. Keep every fact, robot id (in **bold**), place and "
                        "number; add nothing, remove nothing. Be concise and friendly. Markdown "
                        "allowed."
                    ),
                    HumanMessage(text),
                ]
            )
    except Exception as exc:
        log.info("narration polish unavailable: %s", exc)
        return text
    rewritten = reply.content if isinstance(reply.content, str) else ""
    if not rewritten.strip() or any(r not in rewritten for r in robots):
        return text
    return rewritten
