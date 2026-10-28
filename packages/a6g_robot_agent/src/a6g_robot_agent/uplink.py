"""Upstream report delivery (robot -> coordinator)."""

import logging
from collections.abc import Callable
from typing import Any, Protocol

from a6g_common.messages import NL
from a6g_common.schemas import Report
from a6g_transport.messaging import A2AUnavailable, A6GMessage, MessageType, MessagingClient

log = logging.getLogger(__name__)


class Uplink(Protocol):
    async def send(self, report: Report, nl: dict[str, Any] | None = None) -> bool:
        """Deliver one report; False if it could not be delivered (it stays queued)."""
        ...


class A2AUplink:
    def __init__(self, client: MessagingClient) -> None:
        self.client = client

    async def send(self, report: Report, nl: dict[str, Any] | None = None) -> bool:
        try:
            reply = await self.client.send(
                A6GMessage(
                    MessageType.REPORT, report.model_dump(mode="json"), NL(**nl) if nl else None
                )
            )
        except A2AUnavailable as exc:
            log.info("report %s not delivered: %s", report.report_id, exc)
            return False
        return reply.type is MessageType.REPORT_ACK


class RecordingUplink:
    """In-memory uplink for tests and the simulator; ``connected()`` models the 5G link."""

    def __init__(self, connected: Callable[[], bool] = lambda: True) -> None:
        self.connected = connected
        self.reports: list[Report] = []
        self.nl: dict[str, dict[str, Any]] = {}  # report_id -> its nl fields

    async def send(self, report: Report, nl: dict[str, Any] | None = None) -> bool:
        if not self.connected():
            return False
        self.reports.append(report)
        if nl:
            self.nl[report.report_id] = nl
        return True

    def of_kind(self, kind: str) -> list[Report]:
        return [r for r in self.reports if r.kind == kind]
