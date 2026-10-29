"""Network-branch hook. The integration with the real network replaces this.

* ``NetworkBranchClient``: the stand-in that grants every flow at once
  (``network.mode: always_grant``, the default the tests rely on).
* ``CapacityNetwork``: the capacity model of the ``noa_stub``, code only: flows are granted,
  degraded or rejected from the cell's uplink capacity and the coverage level where they
  stream. Emulated, never "measured on the campus network".

Which component checks robot/network consistency is still to be decided.
"""

import logging
import math
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


class NetworkBranchClient:
    """Stand-in: records the flows the allocation implies; reports nominal achievability."""

    mode = "always_grant"

    def __init__(self) -> None:
        self.requests: list[tuple[str, list[dict[str, Any]]]] = []

    async def request_profile(self, mission_id: str, flows: list[dict[str, Any]]) -> None:
        self.requests.append((mission_id, flows))
        for flow in flows:
            log.info("network profile request [%s]: %s", mission_id, flow)

    async def get_achievable(self, mission_id: str) -> dict[str, Any]:
        return {"mission_id": mission_id, "status": "nominal", "source": "stub"}

    async def request_flows(
        self, mission_id: str, flows: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Request each flow; the answer per flow_id is ``{state, granted, reason}``.
        Stand-in: every request is granted at once, at its nominal level."""
        await self.request_profile(mission_id, flows)
        return {
            f["flow_id"]: {"state": "granted", "granted": {"nominal": 1.0}, "reason": "stub"}
            for f in flows
        }

    async def release_flows(self, mission_id: str, flow_ids: list[str]) -> None:
        for fid in flow_ids:
            log.info("network flow release [%s]: %s", mission_id, fid)


class CapacityNetwork(NetworkBranchClient):
    """The ``noa_stub`` capacity model, deterministic.

    Each flow is evaluated where it will stream (the POI or post, not where the robot is
    now): serving cell, SINR and level from the coverage map. Guaranteed and burst flows are
    admitted in order of task priority, then POI risk, then ``flow_id``; one is granted if
    its rate fits the robot's maximum uplink there (``uplink_rate``), its RTT requirement
    fits the level's ``rtt_ms``, and its cost (``rate x SE(anchor) / SE(SINR)``) fits the
    cell's remaining capacity; otherwise it is rejected (``coverage`` or ``capacity``).
    Best-effort flows share what is left and are degraded (with the achieved rate) when they
    get less than they asked for. ``accounting_only`` (the ``reconcile: off`` baseline):
    every request is granted as asked, and the cell shares capacity among the active streams
    by weighted fair share (``fair_share``).
    """

    mode = "capacity"

    def __init__(
        self,
        net: Any,
        coverage: Any,
        where: Callable[[dict[str, Any]], tuple[float, float] | None],
        accounting_only: bool = False,
    ) -> None:
        super().__init__()
        self.net = net
        self.coverage = coverage
        self.where = where  # flow -> (x, y) where it streams (None: nowhere specific)
        self.accounting_only = accounting_only
        self.active: dict[str, dict[str, Any]] = {}  # flow_id -> admission record
        self.reports: list[dict[str, Any]] = []  # AchievableServiceReport, per round
        self.events: list[dict[str, Any]] = []  # NetworkEvent, on re-evaluation
        cap = net.capacity.cells
        self.capacity = {cid: float(c.get("ul_capacity_mbps", 0.0)) for cid, c in cap.items()}
        ref = net.uplink_rate.se(net.uplink_rate.anchor["sinr_db"])
        self._se_ref = ref if ref > 0 else 1.0

    # ------------------------------------------------------------------ the radio

    def at(self, flow: dict[str, Any]) -> dict[str, Any]:
        """Serving cell, SINR and level where the flow streams."""
        xy = self.where(flow)
        if xy is None or self.coverage is None:
            cell = next(iter(self.capacity), "cell")
            return {"cell": cell, "sinr": self.net.uplink_rate.anchor["sinr_db"], "level": "good"}
        s = self.coverage.nearest(*xy)
        if s is None:
            return {"cell": next(iter(self.capacity), "cell"), "sinr": -10.0, "level": "lost"}
        sinr = float(s["sinr"]) + self.net.sinr_shift_db
        return {"cell": s.get("cell"), "sinr": sinr, "level": s["level"]}

    def cost(self, rate_mbps: float, sinr_db: float) -> float:
        """Cell units (= Mbps at the anchor or better) of streaming ``rate`` at ``sinr``."""
        se = self.net.uplink_rate.se(sinr_db)
        return math.inf if se <= 0 else rate_mbps * self._se_ref / se

    def used(self, cell: str, kinds: tuple[str, ...] = ("guaranteed", "burst")) -> float:
        return sum(
            a["cost"] for a in self.active.values() if a["cell"] == cell and a["kind"] in kinds
        )

    # ------------------------------------------------------------------ admission

    def _requirement(self, cls: str) -> Any:
        return self.net.capacity.flow_requirements.get(cls)

    def _kind(self, req: Any) -> str:
        if req is None or req.always or req.ul_mbps <= 0:
            return "always"
        if req.best_effort:
            return "best_effort"
        if req.max_s is not None:
            return "burst"
        return "guaranteed"

    async def request_flows(
        self, mission_id: str, flows: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        await self.request_profile(mission_id, flows)
        answers: dict[str, dict[str, Any]] = {}
        order = sorted(
            flows,
            key=lambda f: (-int(f.get("priority") or 0), -float(f.get("risk") or 0), f["flow_id"]),
        )
        best: list[dict[str, Any]] = []
        for f in order:
            req = self._requirement(f["flow_class"])
            kind = self._kind(req)
            radio = self.at(f)
            rate = float(req.ul_mbps) if req is not None else 0.0
            rec = {
                "flow_id": f["flow_id"],
                "robot_id": f.get("robot_id"),
                "flow_class": f["flow_class"],
                "kind": kind,
                "rate": rate,
                # where it streams, kept for re-evaluation on a level or cell change
                **{k: f[k] for k in ("x", "y") if k in f},
                **radio,
                "cost": 0.0 if kind == "always" else self.cost(rate, radio["sinr"]),
            }
            if kind == "always":
                self.active[f["flow_id"]] = rec
                answers[f["flow_id"]] = self._answer("granted", rate, radio, "telemetry")
                continue
            if self.accounting_only:  # reconcile: off (experiment baseline)
                self.active[f["flow_id"]] = rec
                answers[f["flow_id"]] = self._answer("granted", rate, radio, "no admission")
                continue
            if kind == "best_effort":
                best.append(rec)
                continue
            level_rtt = self.net.link_profiles.get(radio["level"]) or None
            rtt = level_rtt.rtt_ms if level_rtt is not None else None
            reason = None
            if radio["level"] == "lost":
                reason = "coverage"
            elif rate > self.net.uplink_rate.rate_mbps(radio["sinr"]) + 1e-9:
                reason = "coverage"  # the location is too poor for this rate
            elif req.max_rtt_ms is not None and rtt is not None and rtt > req.max_rtt_ms:
                reason = "coverage"
            elif (
                self.used(radio["cell"]) + rec["cost"]
                > self.capacity.get(radio["cell"], 0.0) + 1e-9
            ):
                reason = "capacity"
            if reason is None:
                self.active[f["flow_id"]] = rec
                answers[f["flow_id"]] = self._answer("granted", rate, radio, "admitted")
            else:
                answers[f["flow_id"]] = self._answer("rejected", 0.0, radio, reason)
        for rec in best:  # best effort: what is left, shared in proportion to the asks
            self.active[rec["flow_id"]] = rec
        answers.update(self._share_best_effort({r["flow_id"] for r in best}))
        self.reports.append(self.report(mission_id))
        return answers

    def _answer(
        self, state: str, rate: float, radio: dict[str, Any], reason: str
    ) -> dict[str, Any]:
        return {
            "state": state,
            "granted": {"ul_mbps": round(rate, 3), "sinr_db": radio["sinr"]},
            "reason": f"{reason} ({radio['level']}, cell {radio['cell']})",
        }

    def _share_best_effort(self, new: set[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        by_cell: dict[str, list[dict[str, Any]]] = {}
        for rec in self.active.values():
            if rec["kind"] == "best_effort":
                by_cell.setdefault(rec["cell"], []).append(rec)
        for cell, recs in by_cell.items():
            left = max(self.capacity.get(cell, 0.0) - self.used(cell), 0.0)
            asked = sum(r["cost"] for r in recs)
            share = 1.0 if asked <= left or asked <= 0 else left / asked
            for r in recs:
                achieved = r["rate"] * share
                r["achieved"] = achieved
                if r["flow_id"] in new:
                    state = "granted" if share >= 1.0 else "degraded"
                    out[r["flow_id"]] = {
                        "state": state,
                        "granted": {"ul_mbps": round(achieved, 3), "sinr_db": r["sinr"]},
                        "reason": f"best effort {share:.0%} ({r['level']}, cell {cell})",
                    }
        return out

    async def release_flows(self, mission_id: str, flow_ids: list[str]) -> None:
        await super().release_flows(mission_id, flow_ids)
        for fid in flow_ids:
            self.active.pop(fid, None)
        self._share_best_effort(set())

    # ------------------------------------------------------------------ re-evaluation

    def reevaluate(self) -> list[dict[str, Any]]:
        """After a change of serving cell or level at an active streaming location: the
        affected flows (``NetworkEvent``), which trigger ``reconcile`` on the coordinator."""
        events = []
        for fid, rec in sorted(self.active.items()):
            radio = self.at(rec)
            if (radio["cell"], radio["level"]) != (rec["cell"], rec["level"]):
                events.append(
                    {
                        "flow_id": fid,
                        "from": [rec["cell"], rec["level"]],
                        "to": [radio["cell"], radio["level"]],
                    }
                )
                rec.update(radio, cost=self.cost(rec["rate"], radio["sinr"]))
        self.events += events
        return events

    # ------------------------------------------------------------------ reports

    def fair_share(self) -> dict[str, dict[str, Any]]:
        """Weighted fair share of each cell among the active streams (in cell units): the
        effective rate of each stream and whether it is usable (at least its class needs)."""
        out: dict[str, dict[str, Any]] = {}
        by_cell: dict[str, list[dict[str, Any]]] = {}
        for rec in self.active.values():
            if rec["kind"] != "always":
                by_cell.setdefault(rec["cell"], []).append(rec)
        for cell, recs in by_cell.items():
            cap = self.capacity.get(cell, 0.0)
            demand = sum(r["cost"] for r in recs if math.isfinite(r["cost"]))
            share = 1.0 if demand <= cap or demand <= 0 else cap / demand
            for r in recs:
                eff = 0.0 if not math.isfinite(r["cost"]) else r["rate"] * share
                # a robot cannot send more than its uplink allows where it stands
                eff = (
                    0.0
                    if r["level"] == "lost"
                    else min(eff, self.net.uplink_rate.rate_mbps(r["sinr"]))
                )
                out[r["flow_id"]] = {
                    "cell": cell,
                    "class": r["flow_class"],
                    "robot_id": r["robot_id"],
                    "effective_mbps": round(eff, 3),
                    "usable": eff + 1e-9 >= r["rate"],
                    "oversubscription": round(demand / cap, 3) if cap else math.inf,
                }
        return out

    def report(self, mission_id: str) -> dict[str, Any]:
        """``AchievableServiceReport``: per cell, capacity, used, and the flows."""
        cells = {}
        for cell, cap in self.capacity.items():
            recs = [r for r in self.active.values() if r["cell"] == cell]
            cells[cell] = {
                "ul_capacity_mbps": cap,
                "used_guaranteed_units": round(self.used(cell), 3),
                "demand_units": round(sum(r["cost"] for r in recs if math.isfinite(r["cost"])), 3),
                "flows": len(recs),
            }
        return {
            "mission_id": mission_id,
            "mode": "accounting_only" if self.accounting_only else "capacity",
            "cells": cells,
        }

    async def get_achievable(self, mission_id: str) -> dict[str, Any]:
        return self.report(mission_id)
