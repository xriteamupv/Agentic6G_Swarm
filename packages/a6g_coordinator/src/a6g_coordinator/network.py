"""Network-branch hook (SPEC 10.3). The real I6/I11 integration replaces this stub.

TODO(UPV): decide which component checks robot/network consistency.
"""

import logging
from typing import Any

log = logging.getLogger(__name__)


class NetworkBranchClient:
    """Stand-in: records the flows the allocation implies; reports nominal achievability."""

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
        """SPEC A3.7: request each flow; the answer per flow_id is ``{state, granted,
        reason}``. Stand-in until C5: every request is granted at once, at its nominal
        level."""
        await self.request_profile(mission_id, flows)
        return {
            f["flow_id"]: {"state": "granted", "granted": {"nominal": 1.0}, "reason": "stub"}
            for f in flows
        }

    async def release_flows(self, mission_id: str, flow_ids: list[str]) -> None:
        for fid in flow_ids:
            log.info("network flow release [%s]: %s", mission_id, fid)
