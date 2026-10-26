"""Robot-to-robot links for contract-net (SPEC 9.3): direct A2A, never via the broker."""

from collections.abc import Mapping
from typing import Any, Protocol

from a6g_common.messages import NL
from a6g_common.schemas import Award, Bid, CallForProposals
from a6g_transport.messaging import A6GMessage, MessageType, MessagingClient


class PeerLink(Protocol):
    async def cfp(self, cfp: CallForProposals, nl: dict[str, Any] | None = None) -> Bid: ...

    async def award(self, award: Award) -> dict[str, Any]: ...


class PeerDirectory(Protocol):
    def peer_ids(self) -> list[str]: ...

    def link(self, robot_id: str) -> PeerLink: ...


class A2APeer:
    def __init__(self, client: MessagingClient) -> None:
        self.client = client

    async def cfp(self, cfp: CallForProposals, nl: dict[str, Any] | None = None) -> Bid:
        reply = await self.client.send(
            A6GMessage(MessageType.CFP, cfp.model_dump(mode="json"), NL(**nl) if nl else None)
        )
        return Bid.model_validate(reply.payload)

    async def award(self, award: Award) -> dict[str, Any]:
        reply = await self.client.send(A6GMessage(MessageType.AWARD, award.model_dump(mode="json")))
        return reply.payload


class A2APeerDirectory:
    """Peers from fleet.yaml (``peers`` and their ``a2a_url``)."""

    def __init__(
        self, urls: Mapping[str, str], timeout_s: float = 1.5, *, self_id: str = "robot"
    ) -> None:
        self._links = {
            rid: A2APeer(MessagingClient(url, sender=self_id, receiver=rid, timeout_s=timeout_s))
            for rid, url in urls.items()
        }

    def peer_ids(self) -> list[str]:
        return sorted(self._links)

    def link(self, robot_id: str) -> PeerLink:
        return self._links[robot_id]
