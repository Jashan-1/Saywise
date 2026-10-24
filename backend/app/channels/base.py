from typing import Protocol


class Channel(Protocol):
    """How SaySo reaches a person. Slack and WhatsApp would be further implementations."""

    async def send(self, to: str, text: str) -> str:
        """Send `text` to an address and return the provider's message id."""
        ...


class Router:
    """One Channel for callers: email addresses go by email, everything else by SMS."""

    def __init__(self, sms: Channel, email: Channel) -> None:
        self.sms, self.email = sms, email

    async def send(self, to: str, text: str) -> str:
        return await (self.email if "@" in to else self.sms).send(to, text)
