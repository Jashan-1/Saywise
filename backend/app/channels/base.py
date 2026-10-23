from typing import Protocol


class Channel(Protocol):
    """How SaySo reaches a person. Slack and WhatsApp would be further implementations."""

    async def send(self, to: str, text: str) -> str:
        """Send `text` to an E.164 number and return the provider's message id."""
        ...
