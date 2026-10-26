"""Coordinator -> robot links (SPEC 10.2 publish_envelopes, 11 A2A)."""

from typing import Any, Protocol

from a6g_common.schemas import Envelope, EnvelopeReply, RobotState
from a6g_transport.messaging import A6GMessage, MessageType, MessagingClient


class RobotLink(Protocol):
    async def send_envelope(self, envelope: Envelope) -> EnvelopeReply: ...

    async def get_state(self) -> RobotState: ...


class A2ARobotLink:
    def __init__(
        self,
        base_url: str,
        timeout_s: float = 5.0,
        *,
        robot_id: str | None = None,
        httpx_client: Any = None,
    ) -> None:
        self.client = MessagingClient(
            base_url,
            sender="coordinator",
            receiver=robot_id,
            timeout_s=timeout_s,
            httpx_client=httpx_client,
        )

    @classmethod
    def with_client(cls, client: MessagingClient) -> "A2ARobotLink":
        """A link over an existing client (e.g. an in-process one)."""
        link = cls.__new__(cls)
        link.client = client
        return link

    async def send_envelope(self, envelope: Envelope) -> EnvelopeReply:
        reply = await self.client.send(
            A6GMessage(MessageType.ENVELOPE, envelope.model_dump(mode="json"))
        )
        return EnvelopeReply.model_validate(reply.payload)

    async def get_state(self) -> RobotState:
        reply = await self.client.send(A6GMessage(MessageType.STATE, {}))
        return RobotState.model_validate(reply.payload)

    async def get_queue(self) -> dict[str, Any]:
        reply = await self.client.send(A6GMessage(MessageType.QUEUE, {}))
        return reply.payload


class A2ALinkFactory:
    """Links by the ``a2a_url`` recorded for each robot in the mission's fleet view."""

    def __init__(self, timeout_s: float = 5.0) -> None:
        self.timeout_s = timeout_s
        self._links: dict[str, A2ARobotLink] = {}

    def __call__(self, robot_id: str, entry: dict[str, Any]) -> RobotLink:
        url = entry["a2a_url"]
        link = self._links.get(url)
        if link is None:
            link = self._links[url] = A2ARobotLink(url, self.timeout_s, robot_id=robot_id)
        return link
